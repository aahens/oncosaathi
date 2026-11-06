"""OncoSaathi server: WhatsApp-style chat, live call, patient memory, reminders, and tools for the Sarvam voice agent.

Data flow (privacy): the patient record lives only in patient_store (data/oncosaathi.db, encrypted). Browsers and the
Sarvam agent hold an opaque session token, never a phone number. The agent reads the journey only through /tools/*,
which need TOOLS_API_KEY and return one short answer at a time.

Run:  .venv/bin/python server.py          (SHARE=1 for a public gradio.live link)
CALL_ENGINE=sarvam_agent (default) uses the hosted Samvaad agent; CALL_ENGINE=local uses voice_call.py + brain.py.
"""
import asyncio
import base64
import io
import json
import os
import re
import secrets
import threading
import time
import zipfile

import httpx
import uvicorn
from fastapi import FastAPI, File, Form, Header, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response

import brain
import core
import patient_store as store
import reminders
import route_map
import samvaad_call
import voice_call
import wa_call
import whatsapp

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", "7860"))
SARVAM = "https://api.sarvam.ai"
KEY = core.SARVAM_KEY
TOOLS_KEY = os.environ.get("TOOLS_API_KEY", "").strip()
STAFF_KEY = os.environ.get("STAFF_API_KEY", "").strip()
# Who answers calls, on the web page and in WhatsApp: "sarvam_agent" (the hosted agent, samvaad_call.py) or "local"
# (our own pipeline, voice_call.py). Messages, maps, slips and memory are always ours.
CALL_ENGINE = os.environ.get("CALL_ENGINE", "sarvam_agent").strip().lower()
STATES = {}          # session token -> conversation state (pid, mode, awaiting, where, place)

app = FastAPI()

# Public-link guard: every call, message and slip spends Sarvam credits, so cap use per visitor (per hour) and overall.
LIMITS = {"session": int(os.environ.get("LIMIT_SESSIONS_PER_HOUR", "10")),
          "message": int(os.environ.get("LIMIT_MESSAGES_PER_HOUR", "120")),
          "call": int(os.environ.get("LIMIT_CALLS_PER_HOUR", "10"))}
MAX_CALLS = int(os.environ.get("MAX_CONCURRENT_CALLS", "3"))
CALL_MINUTES = float(os.environ.get("MAX_CALL_MINUTES", "10"))
HITS = {}
LIVE_CALLS = 0


def client_ip(headers, client):
    fwd = headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (client.host if client else "?")


def over_limit(kind, ip):
    now = time.time()
    recent = [t for t in HITS.get((kind, ip), []) if now - t < 3600]
    if len(recent) >= LIMITS[kind]:
        HITS[(kind, ip)] = recent
        return True
    HITS[(kind, ip)] = recent + [now]
    return False


def busy():
    return JSONResponse({"error": "busy", "message": "Too many requests from this device. Please try again in a while."},
                        status_code=429)



@app.on_event("startup")
async def _start_reminders():
    asyncio.create_task(reminders.loop())


# ---------------- sessions ----------------
def state_for(token, mode="chat"):
    """Conversation state for a session token, or None if the token is unknown/expired."""
    pid = store.patient_for_session(token)
    if not pid:
        return None
    st = STATES.get(token)
    if not st or st.get("pid") != pid:
        st = STATES[token] = {"pid": pid, "mode": mode, "awaiting": None, "where": None, "place": None}
    return st


def signin_needed():
    return JSONResponse({"error": "signin", "message": "Please sign in with your mobile number."}, status_code=401)


# ---------------- Sarvam model helpers ----------------
def speak(text, lang):
    try:
        return core.tts(text, lang)[1]
    except Exception as e:
        print("TTS error:", e)
        return None


def stt_bytes(data, filename, content_type):
    ct = (content_type or "audio/webm").split(";")[0]
    r = httpx.post(f"{SARVAM}/speech-to-text", headers={"api-subscription-key": KEY},
                   files={"file": (filename, data, ct)},
                   data={"model": core.STT_MODEL, "language_code": "unknown", "mode": "codemix",
                         "keyterms": json.dumps(core.KEYTERMS)}, timeout=60)
    r.raise_for_status()
    j = r.json()
    return j.get("transcript", ""), j.get("language_code") or ""


def ocr_bytes(data, filename, content_type):
    """Sarvam Vision (Document AI digitise). The image is held in memory only and never written to disk."""
    H = {"api-subscription-key": KEY}
    r = httpx.post(f"{SARVAM}/doc-ai/v1/job/digitise", headers=H,
                   files={"file": (filename, data, content_type or "image/jpeg")},
                   data={"language": "en-IN", "content_type": "mixed"}, timeout=60)
    r.raise_for_status()
    job = r.json()["job_id"]
    for _ in range(40):
        s = httpx.get(f"{SARVAM}/doc-ai/v1/job/{job}/status", headers=H, timeout=30).json()
        if str(s.get("status") or s.get("job_state")).lower() in ("completed", "partially_completed", "failed", "rejected"):
            break
        time.sleep(1)
    z = httpx.get(f"{SARVAM}/doc-ai/v1/job/{job}/results", params={"format": "html"}, headers=H,
                  timeout=60, follow_redirects=True)
    z.raise_for_status()
    zf = zipfile.ZipFile(io.BytesIO(z.content))
    texts = []
    for n in sorted(zf.namelist()):
        if "/metadata/page_" in n and n.endswith(".json"):
            texts += [b.get("text", "") for b in json.loads(zf.read(n)).get("blocks", [])]
    return "\n".join(texts).strip()


# Slip -> tasks. Only *where to go* is taken from the slip; medicine names and doses are never read, stored or said.
SLIP_STEPS = [
    ("blood", r"\b(cbc|lft|kft|rft|hb|hba1c|tsh|blood|sugar|urine|serum|creatinine|electrolytes)\b", "B-G-SAMPLE", "Blood / urine test", "खून / पेशाब की जाँच"),
    ("biopsy", r"\b(biopsy|slides?|blocks?|histo\w*|hpe|ihc|fnac|cytology)\b", "B-1-PATH", "Biopsy slides / FNAC", "बायोप्सी स्लाइड / FNAC"),
    ("pet", r"\b(pet|pet-?ct|bone scan|gamma)\b", "C-2-PET", "PET-CT scan", "पेट-सीटी स्कैन"),
    ("ct", r"(?<!pet-)(?<!pet )\b(cect|hrct|ncct|ct)\b", "C-1-CT", "CT scan", "सीटी स्कैन"),
    ("mri", r"\bmri\b", "C-1-MRI", "MRI", "एमआरआई"),
    ("xray", r"\b(x-?ray|cxr)\b", "C-G-XRAY", "X-ray", "एक्स-रे"),
    ("mammo", r"\bmammo\w*\b", "C-1-USG", "Mammography", "मैमोग्राफ़ी"),
    ("usg", r"\b(usg|ultrasound|sonography)\b", "USG", "Ultrasound", "अल्ट्रासाउंड"),
    ("endo", r"\b(endoscopy|colonoscopy|ugie|ugi scopy|bronchoscopy)\b", "B-2-ENDO", "Endoscopy", "एंडोस्कोपी"),
    ("pac", r"\b(pac|pre-?ana?esthe\w*)\b", "B-3-PAC", "Pre-anaesthesia check-up", "ऑपरेशन से पहले की जाँच"),
    ("ecg", r"\becg\b", "B-3-CARDIO", "ECG", "ईसीजी"),
    ("echo", r"\b(2d ?echo|echo)\b", "ECHO", "2D Echo", "2D इको"),
    ("pft", r"\bpft\b", "PFT", "Lung function test", "फेफड़ों की जाँच"),
    ("medicine", r"\b(tab|cap|syp|inj|tablet|capsule|syrup)\b\.?|\d+\s?mg\b", "CORE-G-PHARM", "Collect medicines", "दवाइयाँ लेना"),
    # referrals to a department ("referred to radiology", "Ref: Surg Onco") -> that department's desk or OPD
    # the booklet's Radiology department (same answer as typing "where is radiology"); a named scan replaces it below
    ("radiology", r"\b(radiology|radiodiagnosis|radio-diagnosis|imaging)\b|रेडियोलॉजी", "C-G-XRAY", "Radiology department", "रेडियोलॉजी विभाग"),
    ("surg_onc", r"\b(surgical oncology|surg\.? ?onco\w*|onco-?surg\w*|oncology surgery)\b", "A-1-SURG", "Surgical Oncology OPD", "सर्जिकल ऑन्कोलॉजी ओपीडी"),
    ("med_onc", r"\b(medical oncology|med\.? ?onco\w*)\b", "A-1-MED", "Medical Oncology OPD", "मेडिकल ऑन्कोलॉजी ओपीडी"),
    ("rad_onc", r"\b(radiation oncology|radiotherapy|rad\.? ?onco\w*)\b", "A-1-RTC", "Radiation Oncology OPD", "रेडिएशन ऑन्कोलॉजी ओपीडी"),
    ("psych", r"\b(psycho-?oncology|psychiatry|psycholog\w*|counsel+ing)\b", "A-4-PSY", "Counselling", "काउंसलिंग"),
    ("palliative", r"\bpalliative\b", "A-4-PALL", "Palliative care clinic", "पैलिएटिव केयर क्लिनिक"),
    ("nutrition", r"\b(nutrition|dietician|dietitian|diet)\b", "A-4-NUT", "Nutrition clinic", "पोषण क्लिनिक"),
]
TEST_KINDS = {"blood", "biopsy", "pet", "ct", "mri", "xray", "mammo", "usg", "endo", "pac", "ecg", "echo", "pft"}


def slip_steps(text):
    t = text.lower()
    found = [s for s in SLIP_STEPS if re.search(s[1], t)]
    kinds = [f[0] for f in found]
    if any(k in ("ct", "mri", "xray", "mammo", "pet") for k in kinds):
        found = [f for f in found if f[0] != "radiology"]       # the scan itself is named: go straight to its room
    elif "radiology" in kinds and "usg" in kinds:               # "radiology for USG": the booklet has no USG room, so
        rad = next(f for f in found if f[0] == "radiology")     # Radiology first, then the ultrasound step
        found.remove(rad)
        found.insert(kinds.index("usg"), rad)
    if any(s[0] in TEST_KINDS for s in found):
        found.insert(0, ("billing", "", "A-G-BILL", "Add money to your smart card", "स्मार्ट कार्ड में पैसे जमा करना"))
    return found


def slip_reply(found, lang, saved, category=None):
    hi = lang == "hi"
    if not found:
        return (f"इस पर्ची पर मुझे कोई जाँच या विभाग नहीं मिला। कृपया इसे {core.HELP_DESK['hi']} पर दिखाइए।" if hi else
                f"I couldn't find any test or department on this slip. Please show it at {core.HELP_DESK['en']}.")
    lines = ["आपकी पर्ची के हिसाब से रास्ता:" if hi else "Your route from the slip:"]
    for i, (_, _, uid, title, title_hi) in enumerate(found, 1):
        u = brain.PLACES[uid]
        if u.get("unlisted"):                                   # the booklet doesn't say where: never guess, say who to ask
            ask = brain.PLACES[u["ask"]]
            where = (f"बुकलेट में जगह नहीं लिखी है; {ask['name_hi']} पर पूछिए: {core.where_say(ask, lang)}" if hi else
                     f"the booklet doesn't say where; ask at the {ask['name']}: {core.where_say(ask, lang)}")
        else:
            where = core.where_say(u, lang, category) if "block" in u else brain.place_short(u, lang)
        lines.append(f"{i}. {title_hi if hi else title}: {where}")
    if any(s[0] == "medicine" for s in found):
        lines.append("दवा कैसे लेनी है, ये डॉक्टर या फ़ार्मासिस्ट बताएँगे।" if hi else "Your doctor or pharmacist will explain how to take the medicines.")
    lines.append("खाली पेट आना है या नहीं, पर्ची पर देखिए या काउंटर पर पूछिए।" if hi else "For preparation such as fasting, check the slip or ask at the counter.")
    if saved:
        lines.append("✅ ये कदम आपकी याद में सेव हो गए। जब पूछेंगे 'आज क्या करना है', मैं बता दूँगी।" if hi else
                     "✅ Saved to your steps. Ask 'what do I do today?' any time.")
    return "\n".join(lines)


def slip_map(found, st, lang):
    """Map to the first step on the slip (the patient's own location of it), from wherever the patient is."""
    if not found:
        return None
    u = brain.PLACES[found[0][2]]
    dest = route_map.spot_for(u["ask"] if u.get("unlisted") else u["id"], st.get("category"))
    start = st.get("here") or "GATE1"
    if not dest or start == dest:
        return None
    st["walk_to"] = dest
    return brain.map_for(start, dest, lang, bool(st.get("here_told")))


def save_slip(pid, found):
    """The slip's steps go into the patient's list, once each. Only where to go is kept, never the slip itself."""
    existing = {t["unit_id"] for t in store.tasks(pid, "pending", actor="slip")}
    for _, _, uid, title, title_hi in found:
        if uid not in existing:
            store.add_task(pid, title, title_hi, uid, source="slip", actor="slip")


async def slip_answer(found, st, lang, saved=True):
    """The slip's route and map, in the patient's language (written in Hindi or English, translated from English otherwise)."""
    base = "hi" if lang == "hi" else "en"
    out = {"text": slip_reply(found, base, saved=saved and bool(found), category=st.get("category")), "lang": base, "route": "SLIP",
           "actions": []}
    walk = slip_map(found, st, base)
    if walk:
        out["map"] = walk
    return await brain.in_language(out, lang)


# ---------------- pages ----------------
@app.get("/")
@app.get("/chat")
def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"), headers={"Cache-Control": "no-store"})


@app.get("/logo.png")
def logo():
    path = os.path.join(HERE, "static", "logo.png")
    if not os.path.exists(path):
        return JSONResponse({"error": "no logo yet"}, status_code=404)
    return FileResponse(path, headers={"Cache-Control": "no-store"})


@app.get("/map")
def hospital_map():
    return FileResponse(os.path.join(HERE, "static", "map.html"), headers={"Cache-Control": "no-store"})


@app.get("/map/route.jpg")
def route_image(request: Request):
    """The walkthrough map. Only room names are in the link, so it says nothing about who is asking; Twilio fetches it too."""
    start, dest = request.query_params.get("from", ""), request.query_params.get("to", "")
    if not (route_map.is_place(start) and route_map.is_place(dest)) or start == dest:
        return Response(status_code=404)
    return Response(route_map.draw(start, dest), media_type="image/jpeg", headers={"Cache-Control": "public, max-age=3600"})


@app.get("/map/campus.png")
def campus_image():
    """The campus drawing for the hospital map page (building positions are illustrative)."""
    return Response(route_map.png(route_map.campus_base()), media_type="image/png", headers={"Cache-Control": "public, max-age=3600"})


@app.get("/map/floor.png")
def floor_image(request: Request):
    """One building floor with its departments, for the hospital map page."""
    b, f = request.query_params.get("b", ""), request.query_params.get("f", "")
    if (b, f) not in route_map.used_floors():
        return Response(status_code=404)
    return Response(route_map.png(route_map.floor_base(b, f)), media_type="image/png", headers={"Cache-Control": "public, max-age=3600"})


@app.get("/api/places")
def places():
    return {"places": route_map.start_choices(), "floors": route_map.FLOOR_NAMES,
            "used_floors": [{"block": b, "floor": f} for b, f in route_map.used_floors()]}


@app.post("/api/here")
async def here(place: str = Form(...), token: str = Form("")):
    """The chat's "I'm here" picker: the same as telling the agent where you are."""
    st = state_for(token, "chat")
    if not st:
        return signin_needed()
    if not route_map.is_place(place):
        return JSONResponse({"error": "unknown place"}, status_code=400)
    st["here"], st["here_told"] = place, True
    return await brain.here_reply(st)


@app.get("/api/hospital")
def hospital():
    return JSONResponse(core.HOSPITAL, headers={"Cache-Control": "no-store"})


@app.get("/api/stay")
def stay():
    return JSONResponse(brain.STAY, headers={"Cache-Control": "no-store"})


@app.get("/api/config")
def config():
    agent_ready = bool(KEY) if CALL_ENGINE == "local" else samvaad_call.ready()
    return {"agent_ready": agent_ready, "call_engine": CALL_ENGINE, "voice_ready": bool(KEY),
            "hospital": core.HOSPITAL["name"], "whatsapp_reminders": reminders.whatsapp_ready(),
            "whatsapp_chat": whatsapp.ready()}


@app.post("/api/log")
async def client_log(entry: dict):
    print(f"[browser] {entry.get('where')}: {entry.get('message')} | {str(entry.get('ua'))[:80]}", flush=True)
    return {"ok": True}


# ---------------- sign-in, memory, chat ----------------
@app.post("/api/session")
async def session(request: Request, phone: str = Form(...), consent: str = Form("")):
    if over_limit("session", client_ip(request.headers, request.client)):
        return busy()
    if consent not in ("1", "true", "yes", "on"):
        return JSONResponse({"error": "consent", "message": "Please agree so OncoSaathi can remember your steps."}, status_code=400)
    try:
        p = store.create(phone, consent=True, actor="webchat")
    except ValueError:
        return JSONResponse({"error": "phone", "message": "Enter a 10-digit Indian mobile number."}, status_code=400)
    tok = store.new_session(p["id"], "webchat")
    st = state_for(tok, "chat")
    return {"token": tok, "text": brain.opening(st)}


@app.get("/api/hello")
def hello(token: str = ""):
    st = state_for(token, "chat")
    if not st:
        return signin_needed()
    st.update(awaiting=None, where=None, place=None, here=None, here_told=False, walk_to=None)   # a new visit: they've moved
    return {"text": brain.opening(st)}


@app.post("/api/me/delete")
def delete_me(token: str = Form("")):
    pid = store.patient_for_session(token)
    if not pid:
        return signin_needed()
    store.delete_patient(pid, actor="webchat")
    STATES.pop(token, None)
    return {"ok": True}


@app.get("/api/outbox")
def outbox(token: str = ""):
    pid = store.patient_for_session(token)
    if not pid:
        return signin_needed()
    return {"messages": store.pull_outbox(pid)}


async def chat_reply(q, st, stt_lang=None):
    out = await brain.respond(q.strip(), st, stt_lang)
    out["audio_b64"] = await run_in_threadpool(speak, out["text"], out["lang"])
    return out


@app.post("/api/message")
async def message(request: Request, q: str = Form(...), token: str = Form("")):
    if over_limit("message", client_ip(request.headers, request.client)):
        return busy()
    st = state_for(token, "chat")
    if not st:
        return signin_needed()
    return await chat_reply(q, st)


@app.post("/api/voice")
async def voice(request: Request, file: UploadFile = File(...), token: str = Form("")):
    if over_limit("message", client_ip(request.headers, request.client)):
        return busy()
    st = state_for(token, "chat")
    if not st:
        return signin_needed()
    data = await file.read()
    try:
        q, stt_lang = await run_in_threadpool(stt_bytes, data, file.filename or "note.webm", file.content_type)
    except Exception as e:
        return JSONResponse({"error": f"Speech-to-text failed: {e}"}, status_code=502)
    out = await chat_reply(q, st, stt_lang)
    out["transcript"] = q
    return out


@app.post("/api/slip")
async def slip(request: Request, file: UploadFile = File(...), lang: str = Form("hi"), token: str = Form("")):
    if over_limit("message", client_ip(request.headers, request.client)):
        return busy()
    st = state_for(token, "chat")
    if not st:
        return signin_needed()
    data = await file.read()
    try:
        text = await run_in_threadpool(ocr_bytes, data, file.filename or "slip.jpg", file.content_type)
    except Exception as e:
        return JSONResponse({"error": f"Could not read the slip: {e}"}, status_code=502)
    finally:
        del data                                            # the photo is not kept
    lang = st.get("lang") or lang
    found = slip_steps(text)
    save_slip(st["pid"], found)
    out = await slip_answer(found, st, lang)
    out["found"] = [f[3] for f in found]
    out["audio_b64"] = await run_in_threadpool(speak, out["text"], out["lang"])
    return out


# ---------------- staff / nurse navigator (adds appointments, triggers reminders) ----------------
def staff_ok(key):
    return bool(STAFF_KEY) and secrets.compare_digest(key or "", STAFF_KEY)


@app.post("/api/staff/task")
async def staff_task(request: Request, x_staff_key: str = Header("")):
    if not staff_ok(x_staff_key):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    j = await request.json()
    p = store.find_by_phone(j.get("phone", ""), actor="staff")
    if not p:
        return JSONResponse({"error": "no patient with that phone"}, status_code=404)
    if j.get("unit_id") and j["unit_id"] not in brain.PLACES:
        return JSONResponse({"error": "unit_id must be a hospital.json id"}, status_code=400)
    tid = store.add_task(p["id"], j["title"], j.get("title_hi"), j.get("unit_id"), j.get("due_date"), j.get("due_time"),
                         source="staff", actor="staff")
    return {"ok": True, "task_id": tid}


@app.post("/api/staff/remind-now")
async def staff_remind(x_staff_key: str = Header("")):
    if not staff_ok(x_staff_key):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return {"sent": await reminders.run_once(force=True)}


# ---------------- tools for the Sarvam voice agent (server-to-server) ----------------
def tool_state(j, x_api_key):
    ok = bool(TOOLS_KEY) and secrets.compare_digest(x_api_key or "", TOOLS_KEY)
    print(f"[tool] called, key {'ok' if ok else 'MISSING' if not x_api_key else 'WRONG'}, fields={sorted(j)}, "
          f"text={str(j.get('text') or j.get('what') or j.get('name') or '')[:60]!r}", flush=True)
    if not ok:
        return None, JSONResponse({"error": "forbidden"}, status_code=403)
    tok = str(j.get("session") or "").strip()
    st = state_for(tok, "voice") if tok else None
    if st is None:                                  # no/expired session: still answer hospital questions, but no memory
        st = {"pid": None, "mode": "voice", "awaiting": None, "where": None, "place": None}
    st["mode"] = "voice"
    return st, None


def last_user_turn(transcript):
    """Samvaad's Call Transcript variable, when the agent sends that instead of `text`: keep the caller's last turn."""
    if isinstance(transcript, list):
        turns = [t for t in transcript if isinstance(t, dict) and str(t.get("role", "")).lower() in ("user", "caller", "human")]
        return str((turns[-1].get("content") or turns[-1].get("text") or "") if turns else "").strip()
    lines = [l.strip() for l in str(transcript or "").splitlines() if l.strip()]
    for l in reversed(lines):
        m = re.match(r"^\W*(user|caller|human|customer)\W*[:\-]\s*(.+)$", l, re.I)
        if m:
            return m.group(2).strip()
    return lines[-1] if lines else ""


@app.post("/tools/ask")
async def tool_ask(request: Request, x_api_key: str = Header("")):
    """The one tool the agent should call for every caller turn: returns the exact sentence to say."""
    j = await request.json()
    st, err = tool_state(j, x_api_key)
    if err:
        return err
    text = str(j.get("text") or "").strip() or last_user_turn(j.get("transcript"))
    out = await brain.respond(text[:500], st)
    return {"say": out["text"], "lang": out["lang"], "route": out["route"].split(" (")[0]}


@app.post("/tools/journey")
async def tool_journey(request: Request, x_api_key: str = Header("")):
    j = await request.json()
    st, err = tool_state(j, x_api_key)
    if err:
        return err
    lang = "en" if j.get("lang") == "en" else "hi"
    return {"say": brain.say_journey(st, lang, "voice")}


@app.post("/tools/mark-done")
async def tool_done(request: Request, x_api_key: str = Header("")):
    j = await request.json()
    st, err = tool_state(j, x_api_key)
    if err:
        return err
    if not st.get("pid"):
        return {"say": "", "error": "no session"}
    lang = "en" if j.get("lang") == "en" else "hi"
    done = store.mark_done(st["pid"], hint=str(j.get("what", "")), actor="sarvam-tool")
    return {"say": brain.say_journey(st, lang, "voice", just_done=done) if done else ""}


@app.post("/tools/remember-name")
async def tool_name(request: Request, x_api_key: str = Header("")):
    j = await request.json()
    st, err = tool_state(j, x_api_key)
    if err:
        return err
    nm = brain.extract_name(str(j.get("name", "")))
    if st.get("pid") and nm:
        store.update(st["pid"], "sarvam-tool", name=nm)
    return {"ok": bool(nm), "name": nm or ""}


# ---------------- WhatsApp (Twilio sandbox) ----------------
# Same brain as the web chat. Consent works like the web sign-in: nothing about the person is stored until they reply
# "हाँ"/"yes". Before that they can still ask for directions (answered from hospital.json, no memory, nothing saved).
WA_CHATS = {}        # "+91…" -> {"token", "asked", "declined", "anon"}; in memory only
WA_SEEN = {}         # MessageSid -> time, so a Twilio retry isn't answered twice
_WA_TASKS = set()
WA_YES = {"haan", "han", "ha", "haa", "hanji", "haanji", "ji", "yes", "y", "ok", "okay", "agree", "i agree", "हाँ", "हां", "हा",
          "जी", "जी हाँ", "जी हां", "हाँ जी", "हां जी", "ठीक है", "theek hai", "thik hai"}
WA_NO = {"nahi", "nahin", "na", "no", "n", "नहीं", "ना"}
WA_DELETE_RX = r"^\s*(delete( my data)?|data (delete|hatao|mitao)|मेरा डेटा (हटाओ|मिटाओ|हटा दो|मिटा दो)|डेटा (हटाओ|मिटाओ))\s*[.!।]?\s*$"
WA_TEXT = {
    "consent": {"hi": "🔒 क्या मैं आपके अगले कदम (अपॉइंटमेंट, जाँच) याद रखूँ, ताकि अगली बार बता सकूँ? सहमति के लिए \"हाँ\" लिखें। "
                      "आपका नंबर सुरक्षित (एन्क्रिप्टेड) रहता है, कभी भी \"delete\" लिखकर सब मिटा सकते हैं। बिना सहमति के भी रास्ता पूछ सकते हैं।\n\n"
                      "ℹ️ मैं एक AI सेवा हूँ, डॉक्टर की सलाह नहीं। मुझसे गलती हो सकती है, कृपया हेल्प डेस्क से पुष्टि कर लीजिए।",
                "en": "🔒 Shall I remember your next steps (appointments, tests) for next time? Reply \"yes\" to agree. "
                      "Your number is stored encrypted, and you can erase everything any time by sending \"delete\". "
                      "You can ask for directions without agreeing.\n\n"
                      "ℹ️ I'm an AI service, not medical advice. I can make mistakes, so please confirm at the help desk."},
    "thanks": {"hi": "धन्यवाद, सहमति दर्ज हो गई। ", "en": "Thank you, your consent is recorded. "},
    "declined": {"hi": "ठीक है, मैं कुछ याद नहीं रखूँगी। आप फिर भी रास्ता पूछ सकते हैं।",
                 "en": "Okay, I won't remember anything. You can still ask for directions."},
    "deleted": {"hi": "आपका सारा डेटा मिटा दिया गया है।", "en": "All your data has been deleted."},
    "nodata": {"hi": "मेरे पास आपका कोई डेटा सेव नहीं है।", "en": "I don't have any of your data saved."},
    "stt_fail": {"hi": "माफ़ कीजिए, आवाज़ समझ नहीं आई। फिर से भेजिए या लिखकर पूछिए।",
                 "en": "Sorry, I couldn't hear that clearly. Please send it again or type your question."},
    "slip_saved": {"hi": "🔒 आपने पर्ची भेजी, इसलिए ये कदम आपकी याद में सेव कर लिए हैं, ताकि अगली बार बता सकूँ। आपका नंबर सुरक्षित "
                         "(एन्क्रिप्टेड) रहता है, कभी भी \"delete\" लिखकर सब मिटा सकते हैं।\n\n"
                         "ℹ️ मैं एक AI सेवा हूँ, डॉक्टर की सलाह नहीं। मुझसे गलती हो सकती है, कृपया हेल्प डेस्क से पुष्टि कर लीजिए।",
                   "en": "🔒 Since you sent your slip, I've saved these steps so I can remind you later. Your number is stored "
                         "encrypted, and you can erase everything any time by sending \"delete\".\n\n"
                         "ℹ️ I'm an AI service, not medical advice. I can make mistakes, so please confirm at the help desk."},
    "slip_fail": {"hi": f"माफ़ कीजिए, ये फ़ोटो पढ़ नहीं पाई। पर्ची की साफ़, सीधी फ़ोटो फिर से भेजिए, या इसे {core.HELP_DESK['hi']} पर दिखाइए।",
                  "en": f"Sorry, I couldn't read that photo. Please send a clear, straight photo of the slip again, or show it at {core.HELP_DESK['en']}."},
    "unsupported": {"hi": "मैं अभी लिखा हुआ सवाल, वॉइस नोट या पर्ची की फ़ोटो समझ सकती हूँ।",
                    "en": "Right now I understand typed questions, voice notes and photos of a slip."},
}


async def wa_say(key, lang):
    if lang in ("hi", "en"):
        return WA_TEXT[key][lang]
    english = WA_TEXT[key]["en"]
    return await brain.translate(english, "en-IN", core.LANG_CODE[lang]) or english


async def wa_send_reply(to, text, lang, transcript=None, walk=None):
    """Text first, then the walkthrough map as a photo with a caption, then the same words as a voice note (Ogg/Opus).
    WhatsApp doesn't allow a caption on audio."""
    body = (f"🎤 _{transcript}_\n\n{text}" if transcript else text)
    r1 = await whatsapp.send(to, body=body)
    if walk and whatsapp.public_base():
        rm = await whatsapp.send(to, body=walk["caption"], media_url=whatsapp.public_base() + walk["url"])
        print(f"[whatsapp] -> {whatsapp.mask(to)}: map {rm[:300]}", flush=True)
    r2 = "skipped"
    try:
        path, b64 = await run_in_threadpool(core.tts, text, lang, "opus")
        os.remove(path)
        aid = whatsapp.put_audio(base64.b64decode(b64))
        r2 = await whatsapp.send(to, media_url=f"{whatsapp.public_base()}/wa/audio/{aid}.ogg")
    except Exception as e:
        r2 = f"tts error {e!r}"
    print(f"[whatsapp] -> {whatsapp.mask(to)}: text {r1[:300]}, voice {r2[:300]}", flush=True)


async def wa_handle(form):
    try:
        await _wa_handle(form)
    except Exception as e:
        print(f"[whatsapp] error: {e!r}", flush=True)
        await whatsapp.send(form.get("From", ""), body=await wa_say("stt_fail", "hi"))


async def _wa_handle(form):
    to = form.get("From", "")
    phone = store.normalize_phone(to.split(":", 1)[-1])
    chat = WA_CHATS.setdefault(to, {"token": None, "asked": False, "declined": False,
                                    "anon": {"pid": None, "mode": "chat", "awaiting": None, "where": None, "place": None}})
    q, stt_lang, transcript = (form.get("Body") or "").strip(), None, None
    ctype = (form.get("MediaContentType0") or "").lower()
    p = store.find_by_phone(phone, actor="whatsapp") if phone else None
    st = None
    if p and p.get("consent_at"):
        st = state_for(chat["token"], "chat") if chat["token"] else None
        if not st:
            chat["token"] = store.new_session(p["id"], "whatsapp")
            st = state_for(chat["token"], "chat")
    lang = (st or chat["anon"]).get("lang") or "hi"

    if int(form.get("NumMedia") or 0) > 0:
        try:
            data = await whatsapp.fetch_media(form.get("MediaUrl0", ""))
        except Exception as e:
            print(f"[whatsapp] media download failed: {e!r}", flush=True)
            return await whatsapp.send(to, body=await wa_say("stt_fail", lang))
        if ctype.startswith("audio/"):
            try:
                q, stt_lang = await run_in_threadpool(stt_bytes, data, "note.ogg", ctype)
                transcript = q
            except Exception as e:
                print(f"[whatsapp] STT failed: {e!r}", flush=True)
            if not q.strip():
                return await whatsapp.send(to, body=await wa_say("stt_fail", lang))
        elif ctype.startswith("image/") or ctype == "application/pdf":
            # Read for everyone (a photo sent before "yes" used to be dropped, 3 Nov 2026). Sending the slip is the
            # patient's own choice to share it, so it counts as agreeing to save its steps (team decision, 4 Nov 2026),
            # with the same notice as the consent question. Someone who already said "no" in this chat is not saved.
            t0 = time.perf_counter()
            try:
                text = await run_in_threadpool(ocr_bytes, data, "slip.pdf" if ctype == "application/pdf" else "slip.jpg", ctype)
            except Exception as e:
                print(f"[whatsapp] OCR failed: {e!r}", flush=True)
                return await whatsapp.send(to, body=await wa_say("slip_fail", lang))
            finally:
                del data                                    # the photo is not kept
            found = slip_steps(text)
            agreed_now = bool(not st and found and phone and not chat["declined"])
            if agreed_now:
                p = store.create(phone, consent=True, actor="whatsapp-slip")
                chat["token"], chat["asked"] = store.new_session(p["id"], "whatsapp"), True
                st = state_for(chat["token"], "chat")
                st["lang"] = lang
            print(f"[whatsapp] <- {whatsapp.mask(to)} slip photo: read in {time.perf_counter() - t0:.1f} s, "
                  f"steps {[f[2] for f in found] or 'none found'}"
                  f"{' (saved; the photo counts as consent)' if agreed_now else '' if st else ' (not saved)'}", flush=True)
            if st:
                save_slip(st["pid"], found)
            ans = await slip_answer(found, st or chat["anon"], lang, saved=bool(st))
            await wa_send_reply(to, ans["text"], ans["lang"], walk=ans.get("map"))
            if agreed_now:
                await whatsapp.send(to, body=await wa_say("slip_saved", lang))
            return
        else:
            return await whatsapp.send(to, body=await wa_say("unsupported", lang))
    if not q:
        return
    word = re.sub(r"[\s.!।,?]+$", "", q.strip().lower())

    if re.search(WA_DELETE_RX, q, re.I):
        if p:
            store.delete_patient(p["id"], actor="whatsapp")
        WA_CHATS.pop(to, None)
        return await whatsapp.send(to, body=await wa_say("deleted" if p else "nodata", lang))
    if not st and phone and chat["asked"] and word in WA_YES:
        p = store.create(phone, consent=True, actor="whatsapp")
        chat["token"] = store.new_session(p["id"], "whatsapp")
        st = state_for(chat["token"], "chat")
        base = "hi" if lang == "hi" else "en"
        ans = await brain.in_language({"text": WA_TEXT["thanks"][base] + brain.opening(st, base), "lang": base, "route": "CONSENT", "actions": []}, lang)
        return await wa_send_reply(to, ans["text"], ans["lang"])
    if not st and chat["asked"] and word in WA_NO:
        chat["declined"] = True
        return await whatsapp.send(to, body=await wa_say("declined", lang))

    out = await brain.respond(q[:500], st or chat["anon"], stt_lang)
    print(f"[whatsapp] <- {whatsapp.mask(to)} {'voice' if transcript else 'text'}: route {out['route']}", flush=True)
    await wa_send_reply(to, out["text"], out["lang"], transcript, out.get("map"))
    if not st and phone and not chat["asked"] and not chat["declined"]:
        chat["asked"] = True
        await whatsapp.send(to, body=await wa_say("consent", out["lang"]))


@app.post("/whatsapp/twilio")
async def whatsapp_twilio(request: Request):
    form = {k: str(v) for k, v in (await request.form()).items()}
    url = (whatsapp.public_base() or str(request.base_url).rstrip("/")) + request.url.path
    if not whatsapp.valid_signature(url, form, request.headers.get("X-Twilio-Signature", "")):
        print("[whatsapp] rejected: bad or missing X-Twilio-Signature (check TWILIO_AUTH_TOKEN / PUBLIC_URL)", flush=True)
        return Response(status_code=403)
    sid, now = form.get("MessageSid", ""), time.time()
    for k in [k for k, t in WA_SEEN.items() if t < now - 600]:
        WA_SEEN.pop(k, None)
    if sid and sid not in WA_SEEN and whatsapp.twiml_mode():
        WA_SEEN[sid] = now
        box = whatsapp.start_collecting()
        try:
            await asyncio.wait_for(wa_handle(form), whatsapp.TWIML_BUDGET_S)
        except asyncio.TimeoutError:                         # send what is ready: the text and map come before the voice note
            print("[whatsapp] reply cut short at the webhook time limit", flush=True)
        return Response(whatsapp.twiml(box), media_type="text/xml")
    if sid and sid not in WA_SEEN:
        WA_SEEN[sid] = now
        task = asyncio.create_task(wa_handle(form))          # reply via the REST API, so Twilio's 15 s timeout never bites
        _WA_TASKS.add(task)
        task.add_done_callback(_WA_TASKS.discard)
    return Response("<Response/>", media_type="application/xml")


@app.get("/whatsapp/meta")
def whatsapp_meta_verify(request: Request):
    """Meta's one-time handshake when the webhook URL is saved."""
    q = request.query_params
    if whatsapp.valid_meta_verify(q.get("hub.mode"), q.get("hub.verify_token")):
        return Response(q.get("hub.challenge", ""), media_type="text/plain")
    return Response(status_code=403)


@app.post("/whatsapp/meta")
async def whatsapp_meta(request: Request):
    raw = await request.body()
    if not whatsapp.valid_meta_signature(raw, request.headers.get("X-Hub-Signature-256", "")):
        print("[whatsapp] rejected: bad or missing X-Hub-Signature-256 (check WHATSAPP_APP_SECRET)", flush=True)
        return Response(status_code=403)
    now = time.time()
    for k in [k for k, t in WA_SEEN.items() if t < now - 600]:
        WA_SEEN.pop(k, None)
    payload = json.loads(raw)
    for ev in whatsapp.meta_calls(payload):                  # 📞 in WhatsApp: answered by wa_call.py
        task = asyncio.create_task(wa_call.handle(ev, MAX_CALLS, CALL_MINUTES, CALL_ENGINE))
        _WA_TASKS.add(task)
        task.add_done_callback(_WA_TASKS.discard)
    for to, why in whatsapp.meta_failures(payload):
        print(f"[whatsapp] not delivered to {whatsapp.mask(to)}: {why}", flush=True)
    for form in whatsapp.meta_messages(payload):
        sid = form["MessageSid"]
        if sid and sid not in WA_SEEN:                       # Meta can deliver the same message more than once
            WA_SEEN[sid] = now
            task = asyncio.create_task(wa_handle(form))      # answer 200 at once; the reply goes out through the Graph API
            _WA_TASKS.add(task)
            task.add_done_callback(_WA_TASKS.discard)
    return {"status": "received"}


@app.get("/wa/audio/{aid}.ogg")
def whatsapp_audio(aid: str):
    data = whatsapp.get_audio(aid)
    if data is None:
        return Response(status_code=404)
    return Response(data, media_type="audio/ogg")


# ---------------- live call ----------------

@app.websocket("/ws/call")
async def call(ws: WebSocket):
    await ws.accept()
    token = ws.query_params.get("token", "")
    pid = store.patient_for_session(token)
    if not pid:
        await ws.send_json({"type": "error", "message": "Please sign in with your mobile number first."})
        await ws.close()
        return
    global LIVE_CALLS
    if LIVE_CALLS >= MAX_CALLS or over_limit("call", client_ip(ws.headers, ws.client)):
        await ws.send_json({"type": "error", "message": "OncoSaathi is busy right now. Please try again in a few minutes."})
        await ws.close()
        return
    if CALL_ENGINE == "local":
        LIVE_CALLS += 1
        try:
            await asyncio.wait_for(voice_call.run_call(ws, token), CALL_MINUTES * 60)
        except asyncio.TimeoutError:
            print("  [call] ended at the time limit", flush=True)
        finally:
            LIVE_CALLS -= 1
        try:
            await ws.close()
        except Exception:
            pass
        return
    if not samvaad_call.ready():
        await ws.send_json({"type": "error", "message": "Voice agent IDs or key are not set in .env yet."})
        await ws.close()
        return
    # A fresh call-only token: the agent passes it back to /tools/*. It reveals nothing about the person.
    call_token = store.new_session(pid, "sarvam-call")
    try:
        await samvaad_call.run(ws, call_token, state_for(call_token, "voice"))
    finally:
        try:
            await ws.close()
        except Exception:
            pass


def start_share_tunnel():
    from gradio.networking import setup_tunnel
    time.sleep(1.5)
    tok_file = os.path.join(HERE, ".share_token")          # reuse the token so the public link can stay the same
    if not os.path.exists(tok_file):
        with open(tok_file, "w") as f:
            f.write(secrets.token_urlsafe(32))
    url = setup_tunnel("127.0.0.1", PORT, open(tok_file).read().strip(), None, None)
    print(f"* Running on public URL: {url}", flush=True)
    if not whatsapp.PUBLIC_URL and url:
        whatsapp.PUBLIC_URL = url.rstrip("/")
    print(f"* WhatsApp webhook (Twilio sandbox, 'When a message comes in', POST): {whatsapp.PUBLIC_URL}/whatsapp/twilio"
          + ("" if whatsapp.ready() else "   [TWILIO_* not set in .env yet]"), flush=True)


if __name__ == "__main__":
    if os.environ.get("SHARE") == "1":
        threading.Thread(target=start_share_tunnel, daemon=True).start()
    host = os.environ.get("HOST", "127.0.0.1")              # the cloud host sets HOST=0.0.0.0
    print(f"* Running on local URL:  http://127.0.0.1:{PORT}  (call engine: {CALL_ENGINE})", flush=True)
    uvicorn.run(app, host=host, port=PORT, log_level="warning", proxy_headers=True, forwarded_allow_ips="*")
