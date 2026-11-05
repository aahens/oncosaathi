"""WhatsApp through Meta, without a phone: signed fake webhooks -> real brain and TTS; the Graph API calls are mocked.

Run:  python meta_test.py     (uses a temporary database and fake Meta keys; needs SARVAM_API_KEY)
"""
import hashlib
import hmac
import io
import json
import os
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlparse

os.environ["ONCO_DB"] = os.path.join(tempfile.mkdtemp(), "meta.db")
os.environ.update(WHATSAPP_PROVIDER="meta", WHATSAPP_TOKEN="test-token", WHATSAPP_PHONE_NUMBER_ID="123",
                  WHATSAPP_APP_SECRET="test-secret", WHATSAPP_VERIFY_TOKEN="test-verify", TWILIO_REPLY_MODE="",
                  PUBLIC_URL="https://example.trycloudflare.com")
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

import patient_store as store  # noqa: E402
import route_map  # noqa: E402
import server  # noqa: E402
import whatsapp  # noqa: E402

whatsapp.PUBLIC_URL = "https://example.trycloudflare.com"
client = TestClient(server.app)
POSTS, UPLOADS = [], []
fails = n = 0


async def fake_post(payload):
    POSTS.append(payload)
    return f"wamid.{len(POSTS)}"


async def fake_upload(data, mime):
    UPLOADS.append((data, mime))
    return f"media{len(UPLOADS)}"


async def fake_own(url):
    """What the server's own /map/route.jpg and /wa/audio routes return (the real loopback read needs a running server)."""
    parts = urlparse(url)
    if parts.path.endswith(".ogg"):
        return whatsapp.get_audio(parts.path.rsplit("/", 1)[-1][:-4]), "audio/ogg"
    q = parse_qs(parts.query)
    return route_map.draw(q["from"][0], q["to"][0]), "image/jpeg"


SLIP_JPG = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples", "sample_slip.jpg"), "rb").read()


def slip_pdf():
    buf = io.BytesIO()
    Image.open(io.BytesIO(SLIP_JPG)).convert("RGB").save(buf, "PDF")
    return buf.getvalue()


async def fake_fetch(media_id):
    """What a patient sends: the sample slip as a photo or a PDF (read by the real Sarvam OCR), else a fake voice note."""
    return {"slip-jpg": SLIP_JPG, "slip-pdf": slip_pdf()}.get(media_id) or b"not really audio"


whatsapp._meta_post, whatsapp._meta_upload, whatsapp._own_file, whatsapp._meta_fetch = fake_post, fake_upload, fake_own, fake_fetch


def check(name, ok, detail=""):
    global fails
    fails += not ok
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  -> {detail}"))


def signed(raw, secret=b"test-secret"):
    return {"X-Hub-Signature-256": "sha256=" + hmac.new(secret, raw, hashlib.sha256).hexdigest(), "Content-Type": "application/json"}


def webhook(message):
    return json.dumps({"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": {"messages": [message]}}]}]}).encode()


def turn(text=None, message=None):
    global n
    n += 1
    POSTS.clear()
    UPLOADS.clear()
    msg = message or {"from": "919876500003", "id": f"wamid.in{n}", "type": "text", "text": {"body": text}}
    raw = webhook(msg)
    r = client.post("/whatsapp/meta", content=raw, headers=signed(raw))
    assert r.status_code == 200, r.status_code
    for _ in range(300):
        if not server._WA_TASKS:
            break
        time.sleep(0.1)
    return list(POSTS)


with client:
    # 1. the handshake and the signature
    q = {"hub.mode": "subscribe", "hub.challenge": "12345"}
    r = client.get("/whatsapp/meta", params={**q, "hub.verify_token": "test-verify"})
    check("handshake echoes the challenge", r.status_code == 200 and r.text == "12345", r.text)
    check("wrong verify token refused", client.get("/whatsapp/meta", params={**q, "hub.verify_token": "nope"}).status_code == 403)
    raw = webhook({"from": "919876500003", "id": "x", "type": "text", "text": {"body": "hi"}})
    check("missing signature refused", client.post("/whatsapp/meta", content=raw).status_code == 403)
    check("wrong signature refused", client.post("/whatsapp/meta", content=raw, headers=signed(raw, b"wrong")).status_code == 403)
    check("delivery receipts are ignored", client.post("/whatsapp/meta", content=b'{"entry":[{"changes":[{"value":{"statuses":[{}]}}]}]}',
                                                       headers=signed(b'{"entry":[{"changes":[{"value":{"statuses":[{}]}}]}]}')).status_code == 200 and not server._WA_TASKS)

    # 2. a direction: text, then the map as an uploaded photo with a caption, then the voice note
    out = turn("Tata Memorial")
    check("reply goes to the sender's number", out and out[0]["to"] == "919876500003", out)
    check("nothing stored before consent", store.find_by_phone("+919876500003") is None)
    out = turn("Where is the MRI?")
    kinds = [m["type"] for m in out]
    check("text, then map, then voice note", kinds[:3] == ["text", "image", "audio"], kinds)
    check("text has the booklet building", "Golden Jubilee" in out[0]["text"]["body"], out[0])
    check("map has a caption", bool(out[1]["image"].get("caption")), out[1])
    check("map is uploaded, not linked", "id" in out[1]["image"] and "link" not in out[1]["image"], out[1])
    check("map uploaded as a JPEG", UPLOADS[0][1] == "image/jpeg" and UPLOADS[0][0][:2] == b"\xff\xd8", UPLOADS[0][1])
    check("voice note uploaded as Ogg/Opus", UPLOADS[1][1] == "audio/ogg" and UPLOADS[1][0][:4] == b"OggS", UPLOADS[1][1])
    check("no caption on audio", "caption" not in out[2]["audio"], out[2])

    # 3. saying where you are redraws the route
    out = turn("I'm at the pharmacy")
    check("new map after 'I'm at the pharmacy'", any(m["type"] == "image" for m in out) and "pharmacy" in out[0]["text"]["body"].lower(), out)

    # 4. Meta can deliver a message twice: answer once
    msg = {"from": "919876500003", "id": "wamid.same", "type": "text", "text": {"body": "Where is billing?"}}
    first = turn(message=msg)
    again = turn(message=msg)
    check("a repeated delivery is answered once", first and not again, again)

    # 5. other message types
    out = turn(message={"from": "919876500003", "id": "wamid.vid", "type": "video", "video": {"id": "v1", "mime_type": "video/mp4"}})
    check("a video gets a polite reply", out and out[0]["type"] == "text", out)
    out = turn(message={"from": "919876500003", "id": "wamid.like", "type": "reaction", "reaction": {"emoji": "👍"}})
    check("a reaction gets no reply", not out, out)

    # 6. safety and erasing
    out = turn("I have chest pain")
    check("emergency answer, no map", out and not any(m["type"] == "image" for m in out), out)
    turn("yes")
    check("consent stores the person", store.find_by_phone("+919876500003") is not None)
    out = turn("delete")
    check("delete erases the person", store.find_by_phone("+919876500003") is None and out, out)

    # 7. a slip photo is read for everyone (on 3 Nov 2026 one sent before "yes" was dropped), and sending it counts as
    #    agreeing to save its steps (team decision, 4 Nov 2026), with the privacy notice
    p4 = "919876500004"
    out = turn(message={"from": p4, "id": "wamid.slip1", "type": "image", "image": {"id": "slip-jpg", "mime_type": "image/jpeg"}})
    body = out[0]["text"]["body"] if out and out[0]["type"] == "text" else ""
    check("slip photo from a new person is read", "सीटी" in body and "स्मार्ट कार्ड" in body, out[:1])
    check("...with the map to the first step", any(m["type"] == "image" for m in out), [m["type"] for m in out])
    p = store.find_by_phone("+" + p4)
    units = {t["unit_id"] for t in store.tasks(p["id"], "pending", actor="test")} if p else set()
    check("...and its steps are saved without asking 'yes'", p and p.get("consent_at") and {"B-G-SAMPLE", "C-1-CT"} <= units, units)
    check("...with the notice: encrypted, 'delete' erases, AI not a doctor",
          out and out[-1]["type"] == "text" and all(w in out[-1]["text"]["body"] for w in ("delete", "एन्क्रिप्टेड", "AI")), out[-1:])
    p5 = "919876500005"
    turn(message={"from": p5, "id": "wamid.no1", "type": "text", "text": {"body": "Where is billing?"}})
    turn(message={"from": p5, "id": "wamid.no2", "type": "text", "text": {"body": "नहीं"}})
    out = turn(message={"from": p5, "id": "wamid.no3", "type": "image", "image": {"id": "slip-jpg", "mime_type": "image/jpeg"}})
    check("someone who said 'no' still gets the slip read", out and "CT" in out[0]["text"]["body"].upper(), out[:1])
    check("...but nothing is saved for them", store.find_by_phone("+" + p5) is None)
    out = turn(message={"from": p4, "id": "wamid.slip3", "type": "document", "document": {"id": "slip-pdf", "mime_type": "application/pdf"}})
    check("a slip sent as a PDF is read too", out and "सीटी" in out[0]["text"].get("body", ""), out[:1])
    real, server.ocr_bytes = server.ocr_bytes, lambda *a: (_ for _ in ()).throw(RuntimeError("blurred"))
    out = turn(message={"from": p4, "id": "wamid.slip4", "type": "image", "image": {"id": "slip-jpg", "mime_type": "image/jpeg"}})
    server.ocr_bytes = real
    check("an unreadable photo gets 'send a clearer photo'", out and "फ़ोटो" in out[0]["text"]["body"], out)

print("\nALL PASSED" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
