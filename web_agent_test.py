"""The web page's 📞 answered by the hosted Sarvam agent (CALL_ENGINE=sarvam_agent), without a browser.

A fake browser WebSocket streams spoken questions (Sarvam TTS) to samvaad_call.run(), which talks to the real agent in
.env. Checks: the agent answers, and for each place asked about our own map and written way (from hospital.json) reach
the page during the call, even when the agent itself doesn't know the place (6 Nov 2026: "radiology", "MRI").
Needs SARVAM_AGENT_* in .env. Run: python web_agent_test.py
"""
import asyncio
import base64
import faulthandler
import io
import os
import sys
import tempfile
import time
import wave

os.environ["ONCO_DB"] = os.path.join(tempfile.mkdtemp(), "webagent.db")
import httpx  # noqa: E402

import core  # noqa: E402
import samvaad_call  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
SR = 16000
fails = 0


def check(name, ok, detail=""):
    global fails
    fails += not ok
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  -> {detail}"))


async def speech(text):
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(f"{core.SARVAM}/text-to-speech", headers={"api-subscription-key": core.SARVAM_KEY},
                         json={"text": text, "target_language_code": "hi-IN", "model": core.TTS_MODEL,
                               "speaker": core.TTS_VOICE["hi"], "speech_sample_rate": SR})
        r.raise_for_status()
        with wave.open(io.BytesIO(base64.b64decode(r.json()["audios"][0]))) as w:
            return w.readframes(w.getnframes())


class FakePage:
    """What the browser does: sends the microphone as 16 kHz PCM in 20 ms pieces, and keeps what the server sends."""

    def __init__(self):
        self.got, self.mic, self.closed = [], asyncio.Queue(), False

    async def send_json(self, m):
        self.got.append((time.monotonic(), m))

    async def receive(self):
        if self.closed:
            return {"type": "websocket.disconnect", "code": 1000}
        try:
            chunk = await asyncio.wait_for(self.mic.get(), 0.02)
        except asyncio.TimeoutError:
            chunk = b"\0\0" * (SR // 50)                     # silence between sentences, as a real microphone sends
        await asyncio.sleep(0.02)
        if chunk is None:
            self.closed = True
            return {"type": "websocket.disconnect", "code": 1000}
        return {"type": "websocket.receive", "bytes": chunk}

    def say(self, pcm):
        step = SR // 50 * 2
        for i in range(0, len(pcm), step):
            self.mic.put_nowait(pcm[i:i + step])

    def of(self, kind, since=0):
        return [m for t, m in self.got if m.get("type") == kind and t >= since]


async def wait_for(cond, secs):
    t = time.monotonic()
    while time.monotonic() - t < secs:
        if cond():
            return True
        await asyncio.sleep(0.2)
    return False


async def main():
    faulthandler.dump_traceback_later(300, exit=True)
    if not samvaad_call.ready():
        print("SARVAM_AGENT_* values are not in .env")
        sys.exit(1)
    q_mri, q_radio = await asyncio.gather(speech("मैं टाटा मेमोरियल हॉस्पिटल में हूँ। एमआरआई स्कैन कहाँ होता है?"),
                                          speech("और रेडियोलॉजी डिपार्टमेंट किधर है?"))
    page = FakePage()
    st = {"pid": None, "mode": "voice", "awaiting": None, "where": None, "place": None}
    call = asyncio.create_task(samvaad_call.run(page, "web-test", st))
    bots = lambda since=0: [m["text"] for m in page.of("transcript", since) if "user" not in m["role"].lower()]
    users = lambda since=0: [m["text"] for m in page.of("transcript", since) if "user" in m["role"].lower()]

    check("the agent connects", await wait_for(lambda: page.of("connected"), 20), page.got[-3:])
    await wait_for(lambda: bots(), 20)
    await asyncio.sleep(4)                                   # let the greeting finish

    for label, pcm, to in (("MRI", q_mri, "C-1-MRI"), ("radiology", q_radio, "C-G-XRAY")):
        t = time.monotonic()
        page.say(pcm)
        await wait_for(lambda: users(t) and bots(t), 30)
        ok = await wait_for(lambda: any(m["to"] == to for m in page.of("map", t)), 30)
        print(f"     caller: {users(t)[:1]}\n     agent:  {bots(t)[:2]}")
        maps = [m for m in page.of("map", t) if m["to"] == to]
        check(f"{label}: our map reaches the page during the call", ok, [m.get("to") for m in page.of("map")])
        check(f"{label}: with the written way from the booklet", maps and maps[0].get("text") and "बिल्डिंग" in maps[0]["text"],
              maps[:1])
        await asyncio.sleep(6)

    page.mic.put_nowait(None)
    await asyncio.wait_for(call, 20)
    check("the call ends cleanly", page.of("ended"), page.got[-3:])


asyncio.run(main())
print("\nALL PASSED" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
