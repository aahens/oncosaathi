"""End-to-end call test, no microphone needed.

Synthesises spoken questions with Sarvam TTS, streams them as 16 kHz PCM into /ws/call (server.py, run in-process on a
spare port), and checks the agent's spoken replies against hospital.json facts. Also checks barge-in.
Run:  .venv/bin/python call_test.py
"""
import asyncio
import base64
import io
import json
import os
import sys
import threading
import time
import wave

os.environ.setdefault("CALL_ENGINE", "local")
import tempfile
os.environ["ONCO_DB"] = os.path.join(tempfile.mkdtemp(), "calltest.db")
import httpx
import uvicorn
import websockets

import datetime as dt
import core
import patient_store as store
import server

PORT = 7871
SR = 16000
# (spoken question, language, substrings any of which must appear in the reply)
TURNS = [
    ("मैं टाटा मेमोरियल हॉस्पिटल में हूँ।", "hi", ["टाटा"]),       # every new call: the hospital first
    ("एंडोस्कोपी कहाँ होती है?", "hi", ["B-201"]),
    ("वहाँ क्या लेकर आना है?", "hi", ["अपॉइंटमेंट"]),
    # STT sometimes drops "पेट" and hears "CT scan"; either way the reply must be a real room
    ("पेट सीटी स्कैन के लिए कहाँ जाना है?", "hi", ["C-201", "C-101"]),
    ("कीमो के बाद तेज़ बुखार है", "hi", ["नर्स", "108"]),
    ("Where is the mammography room?", "en", ["C-105"]),
    ("आज मुझे क्या करना है?", "hi", ["ओपीडी", "OPD"]),
]


def silence(ms):
    return b"\0\0" * (SR * ms // 1000)


async def speech(text, lang):
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(f"{core.SARVAM}/text-to-speech", headers={"api-subscription-key": core.SARVAM_KEY},
                         json={"text": text, "target_language_code": "hi-IN" if lang == "hi" else "en-IN",
                               "model": core.TTS_MODEL, "speaker": core.TTS_VOICE["hi" if lang == "hi" else "en"],
                               "speech_sample_rate": SR})
        r.raise_for_status()
        with wave.open(io.BytesIO(base64.b64decode(r.json()["audios"][0]))) as w:
            return w.readframes(w.getnframes())


async def stream(ws, pcm, speed=2.0):
    for i in range(0, len(pcm), 3200):                       # 100 ms frames, like the browser worklet
        await ws.send(pcm[i:i + 3200])
        await asyncio.sleep(0.1 / speed)


async def main():
    threading.Thread(target=lambda: uvicorn.run(server.app, host="127.0.0.1", port=PORT, log_level="warning"),
                     daemon=True).start()
    await asyncio.sleep(1.5)
    audio = await asyncio.gather(*(speech(t, l) for t, l, _ in TURNS))
    msgs, fails = [], 0
    p = store.create("9000000009", actor="test")
    store.update(p["id"], "test", name="Ramesh", hospital="tata-parel")
    store.add_task(p["id"], "OPD review", "ओपीडी में रिपोर्ट दिखाना", "A-1-MED", dt.date.today().isoformat(), "10:00", source="test")
    token = store.new_session(p["id"], "test")
    async with websockets.connect(f"ws://127.0.0.1:{PORT}/ws/call?token={token}", max_size=None) as ws:
        async def reader():
            async for m in ws:
                msgs.append((time.monotonic(), json.loads(m)))
        rd = asyncio.create_task(reader())

        async def wait_bot(n_before, timeout=15):
            t = time.monotonic()
            while time.monotonic() - t < timeout:
                bots = [m for _, m in msgs if m.get("type") == "transcript" and m["role"] == "bot"]
                if len(bots) > n_before:
                    await asyncio.sleep(0.3)               # let its audio arrive
                    return bots[-1]["text"]
                await ws.send(silence(100))
                await asyncio.sleep(0.1)
            return None

        def n_bot():
            return sum(1 for _, m in msgs if m.get("type") == "transcript" and m["role"] == "bot")

        def bot_audio_secs(since):
            return sum(len(base64.b64decode(m["b64"])) / 2 / m["sr"] for t, m in msgs if t >= since and m.get("type") == "audio")

        t = time.monotonic()
        greet = await wait_bot(0)
        print(f"greeting: {greet}")
        await stream(ws, silence(int(bot_audio_secs(t) * 1000) + 300), speed=1)   # let the greeting "play"
        for (q, lang, want), pcm in zip(TURNS, audio):
            n, t = n_bot(), time.monotonic()
            await stream(ws, silence(300) + pcm + silence(900))
            reply = await wait_bot(n)
            heard = [m["text"] for _, m in msgs if m.get("type") == "transcript" and m["role"] == "user"][-1:]
            ok = bool(reply) and any(w.lower() in reply.lower() for w in want)
            fails += not ok
            print(f"{'OK ' if ok else 'BAD'} said: {q}\n    STT : {heard[0] if heard else '-'}\n    bot : {reply}\n"
                  f"    latency (end of speech -> first bot text): {time.monotonic() - t - (len(pcm) / 2 / SR + 1.2) / 2:.1f}s")
            await stream(ws, silence(int(bot_audio_secs(t) * 1000) + 300), speed=1)

        # barge-in: start talking while the bot is answering
        n = n_bot()
        await stream(ws, silence(300) + audio[1] + silence(900))
        await wait_bot(n)
        t = time.monotonic()
        await stream(ws, silence(400) + audio[4] + silence(900))
        interrupted = any(m.get("type") == "event" and "interrupt" in m.get("name", "") for ts, m in msgs if ts >= t)
        fails += not interrupted
        print(f"{'OK ' if interrupted else 'BAD'} barge-in sends an interrupt to the browser")
        rd.cancel()
    print(f"\n{'ALL PASSED' if not fails else f'{fails} FAILED'}")
    sys.exit(1 if fails else 0)


asyncio.run(main())
