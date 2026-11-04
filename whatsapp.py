"""WhatsApp channel: the plumbing. The conversation itself is handled in server.py with the same brain as the web chat,
so answers still come only from hospital.json. WHATSAPP_PROVIDER=meta uses the Meta WhatsApp Cloud API, anything else Twilio.

Meta: phone -> Meta -> POST /whatsapp/meta (JSON, signed with X-Hub-Signature-256) -> the same handler as Twilio, in the
background. Replies go to the Graph API; the map and voice note are uploaded to /media first, so Meta never has to reach
back to this server for them. A WhatsApp voice call arrives on the same webhook and is answered by wa_call.py. .env (never printed): WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_APP_SECRET,
WHATSAPP_VERIFY_TOKEN. Free-form replies are only allowed within 24 h of the patient's last message.

Twilio:

    phone -> Twilio -> POST /whatsapp/twilio (form) -> we return empty TwiML at once, then in the background:
      voice note (audio/ogg) -> saaras:v4 STT -> brain.respond() -> text reply  + bulbul TTS as Ogg/Opus voice note
      -> Twilio REST API (Messages.json); the voice note is served from /wa/audio/<id>.ogg for ~30 min, in memory only.

.env (never printed): TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM (sandbox: whatsapp:+14155238886),
optional PUBLIC_URL (else the SHARE=1 gradio.live link is used). Every webhook must carry a valid X-Twilio-Signature.
A Twilio trial account does not deliver these replies (REST sends fail with error 21654, and TwiML replies were not sent).
"""
import base64
import contextvars
import hashlib
import hmac
import os
import secrets
import time
from urllib.parse import urlparse
from xml.sax.saxutils import escape

import httpx

PUBLIC_URL = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")     # set by server.py from the share link if empty
AUDIO_TTL_S = 1800
TWIML_BUDGET_S = 13                                                   # Twilio gives up on a webhook after 15 s
_audio = {}                                                           # id -> (ogg bytes, expires); never written to disk
_twiml_box = contextvars.ContextVar("twiml_box", default=None)


def _env(k, default=""):
    return os.environ.get(k, default).strip()


def meta_mode():
    return _env("WHATSAPP_PROVIDER").lower() == "meta"


def ready():
    if meta_mode():
        return bool(_env("WHATSAPP_TOKEN") and _env("WHATSAPP_PHONE_NUMBER_ID") and _env("WHATSAPP_APP_SECRET"))
    return bool(_env("TWILIO_ACCOUNT_SID") and _env("TWILIO_AUTH_TOKEN"))


def valid_meta_signature(raw, signature):
    """Meta's scheme: 'sha256=' + HMAC-SHA256(app secret, raw request body)."""
    secret = _env("WHATSAPP_APP_SECRET")
    if not secret or not signature:
        return False
    return hmac.compare_digest("sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest(), signature)


def valid_meta_verify(mode, token):
    expected = _env("WHATSAPP_VERIFY_TOKEN")
    return bool(expected) and mode == "subscribe" and hmac.compare_digest(expected, token or "")


def meta_messages(payload):
    """Meta's webhook JSON as the same simple fields Twilio sends, so server.py has one conversation handler."""
    out = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for m in change.get("value", {}).get("messages", []):
                kind = m.get("type")
                if kind in ("reaction", "system"):                    # not something the patient asked
                    continue
                form = {"From": f"whatsapp:+{m.get('from', '')}", "MessageSid": m.get("id", ""), "Body": "", "NumMedia": "0"}
                if kind == "text":
                    form["Body"] = m.get("text", {}).get("body", "")
                else:
                    media = m.get(kind) if isinstance(m.get(kind), dict) else {}
                    form.update(NumMedia="1", MediaUrl0=f"meta:{media.get('id', '')}", MediaContentType0=media.get("mime_type") or f"{kind}/unknown")
                out.append(form)
    return out


def meta_calls(payload):
    """WhatsApp voice calls from the same webhook (field "calls"): [{"id", "from", "event": connect|terminate, "session"}]."""
    out = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") == "calls":
                out += change.get("value", {}).get("calls", [])
    return out


def meta_failures(payload):
    """Our replies that Meta accepted but could not deliver (status "failed"), as [(recipient, reason)] for the log."""
    out = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for s in change.get("value", {}).get("statuses", []):
                if s.get("status") == "failed":
                    e = (s.get("errors") or [{}])[0]
                    out.append((s.get("recipient_id", ""), f"{e.get('code', '')} {e.get('title', '')} {e.get('error_data', {}).get('details', '')}".strip()))
    return out


async def call_action(call_id, action, sdp=None):
    """pre_accept / accept (with our SDP answer), reject or terminate a WhatsApp call. Returns (ok, detail)."""
    body = {"messaging_product": "whatsapp", "call_id": call_id, "action": action}
    if sdp:
        body["session"] = {"sdp_type": "answer", "sdp": sdp}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{_graph()}/{_env('WHATSAPP_PHONE_NUMBER_ID')}/calls", headers=_bearer(), json=body)
        return r.status_code < 300, r.text[:300]
    except Exception as e:
        return False, repr(e)


def _graph():
    return f"https://graph.facebook.com/{_env('WHATSAPP_GRAPH_VERSION', 'v23.0') or 'v23.0'}"


def _bearer():
    return {"Authorization": f"Bearer {_env('WHATSAPP_TOKEN')}"}


async def _meta_fetch(media_id):
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
        meta = await c.get(f"{_graph()}/{media_id}", headers=_bearer())
        meta.raise_for_status()
        url = meta.json()["url"]
        host = urlparse(url).hostname or ""
        if not (host.endswith(".fbsbx.com") or host.endswith(".whatsapp.net")):   # the token only goes to Meta's own hosts
            raise ValueError("unexpected media host")
        r = await c.get(url, headers=_bearer())
    r.raise_for_status()
    return r.content


async def _own_file(url):
    """The map or voice note this server already serves, read over loopback so a slow tunnel can't delay the reply."""
    local = url.replace(PUBLIC_URL, f"http://127.0.0.1:{os.environ.get('PORT', '7860')}", 1)
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(local)
    r.raise_for_status()
    return r.content, r.headers.get("content-type", "").split(";")[0]


async def _meta_upload(data, mime):
    async with httpx.AsyncClient(timeout=40) as c:
        r = await c.post(f"{_graph()}/{_env('WHATSAPP_PHONE_NUMBER_ID')}/media", headers=_bearer(),
                         data={"messaging_product": "whatsapp", "type": mime},
                         files={"file": ("reply.ogg" if mime == "audio/ogg" else "route.jpg", data, mime)})
    r.raise_for_status()
    return r.json()["id"]


async def _meta_post(payload):
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"{_graph()}/{_env('WHATSAPP_PHONE_NUMBER_ID')}/messages", headers=_bearer(), json=payload)
    if r.status_code >= 300:
        return f"error {r.status_code}: {r.text[:300]}"
    return r.json().get("messages", [{}])[0].get("id", "sent")


async def _meta_send(to, body, media_url):
    payload = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to.split(":")[-1].lstrip("+")}
    try:
        if media_url:
            data, mime = await _own_file(media_url)
            media_id = await _meta_upload(data, mime)
            if mime.startswith("image/"):
                payload.update(type="image", image={"id": media_id, **({"caption": body[:1024]} if body else {})})
            else:
                payload.update(type="audio", audio={"id": media_id})      # WhatsApp allows no caption on audio
        else:
            payload.update(type="text", text={"body": body[:4000]})
        return await _meta_post(payload)
    except Exception as e:
        detail = getattr(getattr(e, "response", None), "text", "")[:300]
        return f"error {e.__class__.__name__} {detail}"


def public_base():
    return PUBLIC_URL


def twiml_mode():
    """TWILIO_REPLY_MODE=twiml answers inside the webhook response. Trial accounts refuse free-form REST sends (error 21654)."""
    return _env("TWILIO_REPLY_MODE").lower() == "twiml"


def start_collecting():
    """From here on, send() in this request queues its messages for the webhook response instead of calling the REST API."""
    box = []
    _twiml_box.set(box)
    return box


def twiml(box):
    parts = []
    for m in box:
        body = f"<Body>{escape(m['body'][:1550])}</Body>" if m.get("body") else ""
        media = f"<Media>{escape(m['media'])}</Media>" if m.get("media") else ""
        parts.append(f"<Message>{body}{media}</Message>")
    return '<?xml version="1.0" encoding="UTF-8"?><Response>' + "".join(parts) + "</Response>"


def valid_signature(url, params, signature):
    """Twilio's scheme: base64(HMAC-SHA1(auth_token, url + each POST key+value sorted by key))."""
    token = _env("TWILIO_AUTH_TOKEN")
    if not token or not signature:
        return False
    s = url + "".join(k + params[k] for k in sorted(params))
    mac = base64.b64encode(hmac.new(token.encode(), s.encode(), hashlib.sha1).digest()).decode()
    return hmac.compare_digest(mac, signature)


async def fetch_media(url):
    """Download an incoming attachment. Only Twilio's or Meta's own media hosts are accepted (no fetching arbitrary URLs)."""
    if url.startswith("meta:"):
        return await _meta_fetch(url[5:])
    if urlparse(url).hostname != "api.twilio.com":
        raise ValueError("unexpected media host")
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
        r = await c.get(url, auth=(_env("TWILIO_ACCOUNT_SID"), _env("TWILIO_AUTH_TOKEN")))
    r.raise_for_status()
    return r.content


async def send(to, body=None, media_url=None):
    """Send one WhatsApp message via Twilio. Returns the message SID, or an error string (never raises)."""
    box = _twiml_box.get()
    if box is not None:
        box.append({"body": body, "media": media_url})
        return "queued in TwiML"
    if meta_mode():
        return await _meta_send(to, body, media_url)
    sid = _env("TWILIO_ACCOUNT_SID")
    data = {"From": _env("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886"), "To": to}
    if body:
        data["Body"] = body[:1550]
    if media_url:
        data["MediaUrl"] = media_url
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json", data=data,
                             auth=(sid, _env("TWILIO_AUTH_TOKEN")))
        return r.json().get("sid", "") if r.status_code < 300 else f"error {r.status_code}: {r.text[:160]}"
    except Exception as e:
        return f"error {e!r}"


def put_audio(ogg):
    now = time.time()
    for k in [k for k, (_, exp) in _audio.items() if exp < now]:
        _audio.pop(k, None)
    aid = secrets.token_urlsafe(18)
    _audio[aid] = (ogg, now + AUDIO_TTL_S)
    return aid


def get_audio(aid):
    item = _audio.get(aid)
    return item[0] if item and item[1] > time.time() else None


def mask(phone):
    return "…" + phone[-2:] if phone and len(phone) > 6 else "?"
