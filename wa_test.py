"""WhatsApp channel test without a phone: signed fake Twilio webhooks -> real STT / brain / TTS; Twilio sending is mocked.

Run:  .venv/bin/python wa_test.py     (uses a temporary database and fake Twilio keys; needs SARVAM_API_KEY)
"""
import base64
import hashlib
import hmac
import os
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

os.environ["ONCO_DB"] = os.path.join(tempfile.mkdtemp(), "wa.db")
os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"] = "ACtest", "test-token"
os.environ["PUBLIC_URL"] = "https://example.gradio.live"
os.environ["TWILIO_REPLY_MODE"] = "api"
from fastapi.testclient import TestClient  # noqa: E402

import core  # noqa: E402
import patient_store as store  # noqa: E402
import server  # noqa: E402
import whatsapp  # noqa: E402

whatsapp.PUBLIC_URL = "https://example.gradio.live"
REAL_SEND = whatsapp.send
SENT = []


async def fake_send(to, body=None, media_url=None):
    SENT.append({"to": to, "body": body, "media": media_url})
    return "SMfake"


VOICE = {}


async def fake_fetch(url):
    return VOICE[url]


whatsapp.send, whatsapp.fetch_media = fake_send, fake_fetch
client = TestClient(server.app)
PHONE = "whatsapp:+919876500001"
fails = 0


def post(params, sign=True):
    url = "https://example.gradio.live/whatsapp/twilio"
    s = url + "".join(k + params[k] for k in sorted(params))
    sig = base64.b64encode(hmac.new(b"test-token", s.encode(), hashlib.sha1).digest()).decode() if sign else "bad"
    return client.post("/whatsapp/twilio", data=params, headers={"X-Twilio-Signature": sig})


n = 0


def turn(body="", media=None):
    global n
    n += 1
    SENT.clear()
    p = {"From": PHONE, "To": "whatsapp:+14155238886", "Body": body, "MessageSid": f"SM{n}", "NumMedia": "0"}
    if media:
        p.update(NumMedia="1", MediaUrl0=f"https://api.twilio.com/media/{n}", MediaContentType0="audio/ogg")
        VOICE[p["MediaUrl0"]] = media
    r = post(p)
    assert r.status_code == 200, r.status_code
    for _ in range(300):                       # the reply goes out in the background
        if not server._WA_TASKS:
            break
        time.sleep(0.1)
    return list(SENT)


def check(name, ok, detail=""):
    global fails
    fails += not ok
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  -> {detail}"))


with client:
    check("bad signature rejected", post({"From": PHONE, "Body": "hi", "MessageSid": "x"}, sign=False).status_code == 403)

    # 1. a stranger asks in Hindi text: she asks which hospital first (the 23 Oct hospital-first rule), consent asked, nothing stored
    out = turn("एंडोस्कोपी कहाँ होती है?")
    text = out[0]["body"] if out else ""
    check("asks which hospital before giving a room", "अस्पताल" in text and "GJB" not in text, text)
    check("no map before the hospital is known", not any("route.jpg" in (m["media"] or "") for m in out), out)
    check("voice note sent as .ogg", any(m["media"] and m["media"].endswith(".ogg") for m in out), out)
    check("consent asked once", any("हाँ" in (m["body"] or "") for m in out), out)
    check("nothing stored before consent", store.find_by_phone("+919876500001") is None)
    aud = [m["media"] for m in out if m["media"]]
    if aud:
        r = client.get(aud[0].replace("https://example.gradio.live", ""))
        check("voice note served as Ogg/Opus", r.status_code == 200 and r.content[:4] == b"OggS" and b"OpusHead" in r.content[:64],
              r.status_code)

    # 2. same question as a WhatsApp voice note (Ogg/Opus)
    path, b64 = core.tts("एंडोस्कोपी कहाँ होती है?", "hi", "opus")
    os.remove(path)
    out = turn(media=base64.b64decode(b64))
    text = out[0]["body"] if out else ""
    check("voice note -> transcript + a reply", "🎤" in text and "एंडोस्कोपी" not in text.split("\n\n")[-1], text)
    check("consent not asked twice", not any("हाँ" in (m["body"] or "") and "🔒" in (m["body"] or "") for m in out), out)

    # 3. consent -> stored, opening flow
    out = turn("हाँ")
    check("consent recorded", (store.find_by_phone("+919876500001") or {}).get("consent_at") is not None)
    check("opening greeting after consent", out and "ओन्कोसाथी" in out[0]["body"], out)
    out = turn("टाटा मेमोरियल, मैं अस्पताल में हूँ")
    check("hospital answer accepted", out and "टाटा" in out[0]["body"], out)
    out = turn("एमआरआई कहाँ होती है?")
    check("signed-in answer from the booklet", out and "गोल्डन जुबली" in out[0]["body"], out)
    photo = next((m for m in out if m["media"] and "/map/route.jpg" in m["media"]), None)
    check("walkthrough map sent as a photo with a caption", bool(photo and photo["body"]), out)
    check("map comes after the text", photo is not None and out.index(photo) == 1, out)
    if photo:
        r = client.get(photo["media"].replace("https://example.gradio.live", ""))
        check("map served as a JPEG", r.status_code == 200 and r.content[:2] == b"\xff\xd8", r.status_code)
    check("made-up room gets no map", client.get("/map/route.jpg?from=GATE1&to=NOPE").status_code == 404)

    # 4. safety still first
    out = turn("chemo ke baad tez bukhar hai")
    check("emergency routed", out and ("108" in out[0]["body"] or "नर्स" in out[0]["body"] or "इमरजेंसी" in out[0]["body"]), out)

    # 5. erase
    out = turn("delete")
    check("delete erases the person", store.find_by_phone("+919876500001") is None and out and "मिटा" in out[0]["body"], out)

    # 6. trial accounts: the same reply, answered inside the webhook response (TwiML), in order, with XML kept safe
    os.environ["TWILIO_REPLY_MODE"] = "twiml"
    whatsapp.send = REAL_SEND
    PHONE = "whatsapp:+919876500002"

    def twiml_turn(body):
        global n
        n += 1
        p = {"From": PHONE, "To": "whatsapp:+14155238886", "Body": body, "MessageSid": f"SM{n}", "NumMedia": "0"}
        r = post(p)
        return r, ET.fromstring(r.text).findall("Message")

    r, msgs = twiml_turn("Tata Memorial")
    check("TwiML reply is XML", r.status_code == 200 and r.headers["content-type"].startswith("text/xml") and len(msgs) >= 1, r.text[:200])
    r, msgs = twiml_turn("Where is the MRI?")
    media = [m.findtext("Media") or "" for m in msgs]
    check("TwiML has text first", "Golden Jubilee" in (msgs[0].findtext("Body") or ""), r.text[:300])
    check("TwiML has the map second, with a caption", "route.jpg" in media[1] and "&amp;" in r.text and msgs[1].findtext("Body"), r.text[:400])
    check("TwiML has the voice note", any(x.endswith(".ogg") for x in media), media)

print("\nALL PASSED" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
