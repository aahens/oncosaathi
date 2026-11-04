"""OncoSaathi demo server.

Run:   uvicorn main:app --reload
Open:  http://127.0.0.1:8000        (browser microphone demo, no WhatsApp needed)
       http://127.0.0.1:8000/docs   (try every endpoint)
WhatsApp webhook URL: https://<your-tunnel>/webhook
"""
import asyncio
import base64
import hashlib
import hmac
import logging
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

import sarvam_voice  # loads .env and the Sarvam client
from router import BUILDINGS, DISCLAIMER, DOCTOR_REPLY, DONT_KNOW, EMERGENCY_REPLY, HOSPITAL, ROOMS, locations_for, pick, reply_for, route

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("oncosaathi")

WA_TOKEN = os.environ.get("WA_ACCESS_TOKEN", "")
WA_PHONE_ID = os.environ.get("WA_PHONE_NUMBER_ID", "")
WA_VERIFY_TOKEN = os.environ.get("WA_VERIFY_TOKEN", "")
WA_APP_SECRET = os.environ.get("WA_APP_SECRET", "")
GRAPH = f"https://graph.facebook.com/{os.environ.get('WA_GRAPH_VERSION', 'v23.0')}"
HELP_DESK = os.environ.get("HELP_DESK_NUMBER", "the hospital help desk")
USE_LLM = os.environ.get("USE_LLM_FALLBACK", "1") == "1"
MIN_LANG_CONFIDENCE = 0.5
WARM_INTENTS = ["registration", "blood_test", "chemo_daycare", "pharmacy", "scan", "pathology"]

seen_ids: deque[str] = deque(maxlen=5000)  # Meta can deliver the same message more than once


async def warm_tts_cache() -> None:
    """Synthesise the most-asked Hindi replies once at startup so the first demo answer is already fast."""
    replies = [ROOMS[i]["reply"] for i in WARM_INTENTS] + [DOCTOR_REPLY, DONT_KNOW, EMERGENCY_REPLY]
    for reply in replies:
        try:
            await run_in_threadpool(sarvam_voice.text_to_speech, reply["hi-IN"], "hi-IN")
        except Exception as e:
            log.warning("TTS warm-up skipped: %s", e.__class__.__name__)
            return
    log.info("TTS cache warmed with %d Hindi replies", len(replies))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(warm_tts_cache())
    yield
    task.cancel()


app = FastAPI(title="OncoSaathi", lifespan=lifespan)
# Lets demo.html work when opened through VS Code Live Server (a different port).
app.add_middleware(CORSMiddleware, allow_origin_regex=r"http://(127\.0\.0\.1|localhost)(:\d+)?",
                   allow_methods=["*"], allow_headers=["*"])


# ---------- the pipeline: listen -> route -> speak ----------

KEYWORD_LANGS = {"hi-IN", "mr-IN", "en-IN"}  # languages the keyword lists in router.py cover
SAFETY_INTENTS = {"clinical", "emergency"}


def reply_language(lang: str) -> str:
    """Answer in the patient's language when Bulbul can speak it, otherwise Hindi."""
    return lang if lang in sarvam_voice.TTS_LANGS else "hi-IN"


async def localise(answer: str, answer_lang: str, target: str) -> tuple[str, str]:
    """Translate an approved reply that has no hand-written version in the target language."""
    if answer_lang == target:
        return answer, answer_lang
    try:
        translated = await run_in_threadpool(sarvam_voice.translate, answer, answer_lang, target)
        if translated:
            return translated, target
    except Exception as e:
        log.warning("translation to %s failed: %s", target, e.__class__.__name__)
    return answer, answer_lang


async def speak(answer: str, answer_lang: str, intent: str) -> dict:
    speech = await run_in_threadpool(sarvam_voice.text_to_speech, answer, answer_lang)
    return {"answer": answer, "intent": intent, "answer_language": answer_lang, "audio_ogg": speech,
            "locations": locations_for(intent)}


async def answer_text(text: str, lang: str = "auto") -> dict:
    if lang == "auto":
        try:
            lang = await run_in_threadpool(sarvam_voice.detect_language, text)
        except Exception as e:
            log.warning("language detection failed: %s", e.__class__.__name__)
            lang = "hi-IN"
    target = reply_language(lang)
    routed_text = text
    if lang not in KEYWORD_LANGS:
        # Keyword and clinical lists only cover Hindi/Marathi/English, so also route on an
        # English translation; otherwise a Tamil "pain ... CBC" question would get directions.
        try:
            english = await run_in_threadpool(sarvam_voice.translate, text, lang, "en-IN")
            routed_text = f"{text} {english}"
        except Exception as e:
            log.warning("question translation failed: %s", e.__class__.__name__)
    classify = sarvam_voice.classify if USE_LLM else None
    answer, intent, answer_lang = await run_in_threadpool(route, routed_text, target, classify)
    if lang not in KEYWORD_LANGS and USE_LLM and intent not in SAFETY_INTENTS:
        # Second safety net for languages our keyword lists don't cover.
        try:
            label = await run_in_threadpool(sarvam_voice.classify, text)
        except Exception as e:
            log.warning("safety classification failed: %s", e.__class__.__name__)
            label = None
        if label in SAFETY_INTENTS:
            intent = label
            answer, answer_lang = reply_for(intent, target)
    answer, answer_lang = await localise(answer, answer_lang, target)
    out = await speak(answer, answer_lang, intent)
    out["language"] = lang
    return out


async def answer_voice(audio: bytes, filename: str) -> dict:
    t0 = time.perf_counter()
    stt = await run_in_threadpool(sarvam_voice.speech_to_text, audio, filename)
    t1 = time.perf_counter()
    heard, lang, prob = stt["transcript"], stt["language_code"], stt["language_probability"]

    if len(heard) < 2 or (prob is not None and prob < MIN_LANG_CONFIDENCE):
        target = reply_language(lang)
        answer, answer_lang = await localise(*pick(DONT_KNOW, target), target)
        out = await speak(answer, answer_lang, "unclear")
    else:
        out = await answer_text(heard, lang)
    t2 = time.perf_counter()
    out.update(heard=heard, language=lang, language_probability=prob,
               seconds={"listen": round(t1 - t0, 2), "think_and_speak": round(t2 - t1, 2), "total": round(t2 - t0, 2)})
    return out


def to_json(out: dict) -> dict:
    out = dict(out)
    out["audio_ogg_base64"] = base64.b64encode(out.pop("audio_ogg")).decode()
    return out


# ---------- local demo endpoints ----------

@app.get("/", include_in_schema=False)
async def demo_page():
    return FileResponse(Path(__file__).with_name("demo.html"))


@app.get("/health")
async def health():
    return {"status": "ok", "whatsapp_configured": bool(WA_TOKEN and WA_PHONE_ID), "llm_fallback": USE_LLM}


@app.get("/hospital")
async def hospital():
    """Building and floor data for the map on the demo page."""
    return {**HOSPITAL["hospital"], "buildings": BUILDINGS, "disclaimer": DISCLAIMER["en-IN"]}


@app.post("/navigate-voice")
async def navigate_voice(audio: UploadFile = File(...)):
    data = await audio.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty audio file")
    return to_json(await answer_voice(data, audio.filename or "voice.ogg"))


class TextQuestion(BaseModel):
    text: str
    lang: str = "auto"


@app.post("/navigate-text")
async def navigate_text(q: TextQuestion):
    t0 = time.perf_counter()
    out = await answer_text(q.text, q.lang)
    out.update(heard=q.text, seconds={"total": round(time.perf_counter() - t0, 2)})
    return to_json(out)


# ---------- WhatsApp Cloud API webhook ----------

@app.get("/webhook")
async def verify(request: Request):
    """Meta's one-time handshake when you save the webhook URL in the dashboard."""
    q = request.query_params
    if WA_VERIFY_TOKEN and q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == WA_VERIFY_TOKEN:
        return PlainTextResponse(q.get("hub.challenge", ""))
    return Response(status_code=403)


@app.post("/webhook")
async def incoming(request: Request, background: BackgroundTasks):
    body = await request.body()
    if WA_APP_SECRET:
        expected = "sha256=" + hmac.new(WA_APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, request.headers.get("X-Hub-Signature-256", "")):
            return Response(status_code=403)
    payload = await request.json()
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for msg in change.get("value", {}).get("messages", []):
                if msg.get("id") in seen_ids:
                    continue
                seen_ids.append(msg.get("id"))
                background.add_task(handle_message, msg)
    return {"status": "received"}  # reply 200 fast; the real work runs in the background


async def handle_message(msg: dict) -> None:
    # The sender's number is used only to reply and is never logged or stored.
    to, kind = msg.get("from"), msg.get("type")
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            if kind == "audio":
                audio = await download_media(http, msg["audio"]["id"])
                out = await answer_voice(audio, "voice.ogg")
            elif kind == "text":
                out = await answer_text(msg["text"]["body"], "auto")
            else:
                await send_text(http, to, "Namaste! Kripya apna sawaal voice note mein bhejiye. / Please send your question as a voice note.")
                return
            log.info("whatsapp %s -> intent=%s timings=%s", kind, out["intent"], out.get("seconds"))
            note = pick(DISCLAIMER, out["answer_language"])[0]
            await send_text(http, to, f"{out['answer']}\n\n{note}")
            media_id = await upload_audio(http, out["audio_ogg"])
            await send_audio(http, to, media_id)
    except Exception as e:
        log.error("whatsapp reply failed: %s", e.__class__.__name__)
        try:
            async with httpx.AsyncClient(timeout=15) as http:
                await send_text(http, to, f"Maaf kijiye, abhi jawab nahi de pa rahe. Kripya {HELP_DESK} par sampark karein.")
        except Exception:
            log.error("fallback text also failed")


def wa_headers() -> dict:
    if not (WA_TOKEN and WA_PHONE_ID):
        raise RuntimeError("WA_ACCESS_TOKEN / WA_PHONE_NUMBER_ID missing in .env")
    return {"Authorization": f"Bearer {WA_TOKEN}"}


async def download_media(http: httpx.AsyncClient, media_id: str) -> bytes:
    meta = await http.get(f"{GRAPH}/{media_id}", headers=wa_headers())
    meta.raise_for_status()
    file = await http.get(meta.json()["url"], headers=wa_headers())
    file.raise_for_status()
    return file.content


async def upload_audio(http: httpx.AsyncClient, ogg: bytes) -> str:
    r = await http.post(f"{GRAPH}/{WA_PHONE_ID}/media", headers=wa_headers(),
                        data={"messaging_product": "whatsapp", "type": "audio/ogg"},
                        files={"file": ("reply.ogg", ogg, "audio/ogg")})
    r.raise_for_status()
    return r.json()["id"]


async def send_message(http: httpx.AsyncClient, to: str, body: dict) -> None:
    r = await http.post(f"{GRAPH}/{WA_PHONE_ID}/messages", headers=wa_headers(),
                        json={"messaging_product": "whatsapp", "recipient_type": "individual", "to": to, **body})
    if r.is_error:
        log.error("WhatsApp send error %s: %s", r.status_code, r.text[:300])
    r.raise_for_status()


async def send_text(http: httpx.AsyncClient, to: str, text: str) -> None:
    await send_message(http, to, {"type": "text", "text": {"body": text}})


async def send_audio(http: httpx.AsyncClient, to: str, media_id: str) -> None:
    await send_message(http, to, {"type": "audio", "audio": {"id": media_id}})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
