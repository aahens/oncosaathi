"""WhatsApp call answered by the hosted Sarvam agent (CALL_ENGINE=sarvam_agent), without a phone.

A fake caller plays Meta's part over real WebRTC on this computer; wa_call.handle() answers it and hands the audio to
samvaad_call.run(), which talks to the real agent in .env. Checks: answered, greeting heard, a spoken question gets a
spoken answer, talking over her stops her, no map from an agent call, hang-up closes the call.
Needs the SARVAM_AGENT_* values in .env. Run: python wa_agent_test.py
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

os.environ["ONCO_DB"] = os.path.join(tempfile.mkdtemp(), "waagent.db")
import httpx  # noqa: E402
import numpy as np  # noqa: E402
from aiortc import RTCPeerConnection, RTCSessionDescription  # noqa: E402
from aiortc.mediastreams import MediaStreamError  # noqa: E402

import core  # noqa: E402
import samvaad_call  # noqa: E402
import wa_call  # noqa: E402
import whatsapp  # noqa: E402

fails = 0
actions, sent, said, events = [], [], [], []


def check(name, ok, detail=""):
    global fails
    fails += not ok
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  -> {detail}"))


async def fake_action(cid, action, sdp=None):
    actions.append((action, sdp))
    return True, "ok"


async def fake_send(to, body=None, media_url=None):
    sent.append((to, body, media_url))
    return "sent"


_send_json = wa_call.Bridge.send_json
_push = wa_call.Speaker.push
gap_log = []                   # (when, how long the voice had run dry) for each gap, to see where it happened


def push_logged(self, pcm):
    before, dry = self.gaps, self.dry_at
    _push(self, pcm)
    if self.gaps > before:
        gap_log.append((time.monotonic(), round(time.monotonic() - dry, 3)))


async def recording_send_json(self, m):
    if m.get("type") == "transcript":
        said.append((time.monotonic(), m["role"], m["text"]))
    elif m.get("type") == "event":
        events.append((time.monotonic(), m["name"]))
    await _send_json(self, m)


async def speech(text):
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(f"{core.SARVAM}/text-to-speech", headers={"api-subscription-key": core.SARVAM_KEY},
                         json={"text": text, "target_language_code": "hi-IN", "model": core.TTS_MODEL,
                               "speaker": core.TTS_VOICE["hi"], "speech_sample_rate": 48000})
        r.raise_for_status()
        with wave.open(io.BytesIO(base64.b64decode(r.json()["audios"][0]))) as w:
            return w.readframes(w.getnframes())


async def wait_for(cond, secs):
    t = time.monotonic()
    while time.monotonic() - t < secs:
        if cond():
            return True
        await asyncio.sleep(0.2)
    return False


async def main():
    faulthandler.dump_traceback_later(240, exit=True)
    if not samvaad_call.ready():
        print("SARVAM_AGENT_* values are not in .env")
        sys.exit(1)
    whatsapp.call_action, whatsapp.send = fake_action, fake_send
    whatsapp.PUBLIC_URL = "https://example.test"
    wa_call.Bridge.send_json = recording_send_json
    wa_call.Speaker.push = push_logged
    question, talk_over = await asyncio.gather(speech("मैं टाटा मेमोरियल हॉस्पिटल में हूँ। सीटी स्कैन कहाँ होता है?"),
                                               speech("रुकिए रुकिए, मुझे फ़ार्मेसी जाना है।"))
    caller = RTCPeerConnection()
    mouth = wa_call.Speaker()
    caller.addTrack(mouth)
    heard = []

    @caller.on("track")
    def on_track(track):
        async def ear():
            while True:
                try:
                    f = await track.recv()
                except MediaStreamError:
                    return
                heard.append((time.monotonic(), float(np.sqrt(np.mean(f.to_ndarray().astype(np.float32) ** 2)))))
        asyncio.create_task(ear())

    await caller.setLocalDescription(await caller.createOffer())
    await wa_call.handle({"id": "wacid.AGENT", "from": "919000000010", "event": "connect",
                          "session": {"sdp_type": "offer", "sdp": caller.localDescription.sdp}}, engine="sarvam_agent")
    check("answered with pre_accept then accept", [a for a, _ in actions] == ["pre_accept", "accept"], actions)
    session = wa_call.CALLS.get("wacid.AGENT")
    await caller.setRemoteDescription(RTCSessionDescription(sdp=actions[-1][1], type="answer"))

    loud = lambda since: sum(0.02 for t, r in heard if t >= since and r > 300)
    bots = lambda since=0: [x for t, r, x in said if r != "user" and t >= since]
    users = lambda since=0: [x for t, r, x in said if r == "user" and t >= since]
    last_loud = lambda: max([t for t, r in heard if r > 300] or [0])

    ok = await wait_for(lambda: loud(0) > 1.0, 30)
    check("caller hears the agent's greeting", ok, f"{loud(0):.1f} s of voice")
    print(f"     greeting: {bots()[:1]}")
    await wait_for(lambda: time.monotonic() - last_loud() > 1.5, 30)

    t1 = time.monotonic()
    mouth.push(b"\0\0" * (48000 // 2) + question + b"\0\0" * 48000)
    ok = await wait_for(lambda: users(t1) and bots(t1), 30)
    print(f"     caller said: {users(t1)[:1]}\n     agent: {bots(t1)[:1]}")
    check("the agent hears the question and answers it", ok, said)
    ok = await wait_for(lambda: loud(t1 + 2) > 1.0, 20)
    check("caller hears the answer", ok, f"{loud(t1 + 2):.1f} s of voice")

    t2 = time.monotonic()
    await wait_for(lambda: loud(t2) > 0.4, 10)                 # she is talking: talk over her
    mouth.push(talk_over + b"\0\0" * 48000)
    ok = await wait_for(lambda: any("interrupt" in n.lower() for t, n in events if t >= t2), 15)
    check("talking over her stops her (the agent's interrupt reaches the phone)", ok, events[-5:])
    await wait_for(lambda: bots(t2), 20)
    print(f"     after talking over: {users(t2)[-1:]} -> {bots(t2)[-1:]}")
    await wait_for(lambda: time.monotonic() - last_loud() > 1.5, 30)

    check("no map is sent from an agent call", not sent, sent)
    t0 = said[0][0] if said else 0
    where = [(round(t - t0, 2), dry, [x[:30] for tt, r, x in said if r != "user" and tt <= t][-1:]) for t, dry in gap_log]
    check("her streamed voice reached the caller with no word cut short", session and session.bridge.speaker.gaps == 0,
          f"{session and session.bridge.speaker.gaps} gaps; (s after first caption, dry for s, last bot line before): {where}")
    actions.clear()
    await wa_call.handle({"id": "wacid.AGENT", "event": "terminate", "status": "COMPLETED", "duration": 30})
    ok = await wait_for(lambda: "wacid.AGENT" not in wa_call.CALLS, 15)
    check("hang-up closes the call", ok)
    check("no terminate sent after the caller hung up", not actions, actions)
    await caller.close()


asyncio.run(main())
print("\nALL PASSED" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
