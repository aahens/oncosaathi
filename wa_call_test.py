"""WhatsApp call test without a phone: a fake caller plays Meta's part over real WebRTC on this computer.

The caller makes an SDP offer like WhatsApp does, wa_call.handle() answers it (Meta's API is replaced by a recorder),
then the caller speaks a question through the call and we check the spoken reply, the map in the chat and hang-up.
Uses real Sarvam STT/TTS. Run: python wa_call_test.py
"""
import asyncio
import base64
import faulthandler
import io
import os
import re
import sys
import tempfile
import time
import wave

os.environ["ONCO_DB"] = os.path.join(tempfile.mkdtemp(), "wacall.db")
import httpx  # noqa: E402
import numpy as np  # noqa: E402
from aiortc import RTCPeerConnection, RTCSessionDescription  # noqa: E402
from aiortc.mediastreams import MediaStreamError  # noqa: E402

import core  # noqa: E402
import patient_store as store  # noqa: E402
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


async def recording_send_json(self, m):
    if m.get("type") == "transcript":
        said.append((m["role"], m["text"]))
    elif m.get("type") == "event":
        events.append(m["name"])
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
    faulthandler.dump_traceback_later(200, exit=True)          # a stuck call fails the test instead of hanging it
    whatsapp.call_action, whatsapp.send = fake_action, fake_send
    whatsapp.PUBLIC_URL = "https://example.test"
    wa_call.Bridge.send_json = recording_send_json
    p = store.create("9000000009", actor="test")
    store.update(p["id"], "test", name="Ramesh", hospital="tata-parel")

    # parsing Meta's webhook
    hook = {"entry": [{"changes": [{"field": "calls", "value": {"calls": [{"id": "wacid.X", "from": "919000000009", "event": "connect",
            "session": {"sdp_type": "offer", "sdp": "v=0"}}]}}, {"field": "messages", "value": {"messages": []}}]}]}
    check("call events are read from the webhook", [c["id"] for c in whatsapp.meta_calls(hook)] == ["wacid.X"])
    check("a call is not mistaken for a message", whatsapp.meta_messages(hook) == [])
    failed = {"entry": [{"changes": [{"field": "messages", "value": {"statuses": [
        {"status": "failed", "recipient_id": "919000000009", "errors": [{"code": 131053, "title": "Media upload error"}]},
        {"status": "delivered", "recipient_id": "919000000009"}]}}]}]}
    check("undelivered replies are reported", whatsapp.meta_failures(failed) == [("919000000009", "131053 Media upload error")],
          whatsapp.meta_failures(failed))
    asks = ["ये मुझे message में मिलेगा?", "Can you send me a message of where to go?", "WhatsApp pe bhej do", "मैसेज कर दो"]
    others = ["एंडोस्कोपी कहाँ होती है?", "Where is the pharmacy?", "मैं टाटा मेमोरियल हॉस्पिटल में हूँ।", "chemo kab hai"]
    check("'send it as a message' is recognised", all(re.search(wa_call.ASK_IN_CHAT, f" {t.lower()} ") for t in asks))
    check("ordinary questions are not", not any(re.search(wa_call.ASK_IN_CHAT, f" {t.lower()} ") for t in others))

    # the hosted agent streams 0.1 s pieces of 16 kHz voice in real time (4 Nov 2026): they must join without clicks,
    # and a piece that arrives late must not leave a gap in the caller's ear
    tone = (3000 * np.sin(2 * np.pi * 440 * np.arange(16000) / 16000)).astype(np.int16).tobytes()
    b = wa_call.Bridge("whatsapp:+919000000009")
    for i in range(0, len(tone), 3200):
        b.to_speaker(tone[i:i + 3200], 16000)
    out = np.frombuffer(bytes(b.speaker.buf), np.int16).astype(np.float32)
    jump, step = float(np.abs(np.diff(out[100:-100])).max()), 3000 * 2 * np.pi * 440 / 48000
    check("0.1 s pieces join smoothly (no click)", jump < 1.3 * step and abs(len(out) - 48000) < 600, f"jump {jump:.0f} vs {step:.0f}, {len(out)} samples")

    spk, ear = wa_call.Speaker(), []
    word = np.concatenate([3000 * np.ones(2880), np.zeros(1920)])   # 0.1 s at 48 kHz: a 60 ms "word", then a 40 ms pause
    piece = word.astype(np.int16).tobytes()

    async def stream_in():
        for k in range(20):
            spk.push(piece)
            await asyncio.sleep(0.1 + (0.15 if k == 4 else 0))   # the 6th piece is 150 ms late; sleep() also runs slow

    async def listen_out():
        for _ in range(200):
            f = await spk.recv()
            ear.append(bool(np.abs(f.to_ndarray()).max()))
    await asyncio.gather(stream_in(), listen_out())
    runs = [len(r) for r in "".join("v" if v else "." for v in ear).split(".") if r]
    check("a piece arriving late cuts no word", runs == [3] * 20 and spk.gaps == 0, f"word lengths {runs}, {spk.gaps} gaps")

    # the phone's sound stops (network gone, laptop asleep): the call must end, not wait for the time limit
    class DeadTrack:
        kind = "audio"

        async def recv(self):
            await asyncio.sleep(3600)

    wa_call.NO_AUDIO_S, keep = 0.5, wa_call.NO_AUDIO_S
    b = wa_call.Bridge("whatsapp:+919000000009")
    await asyncio.wait_for(b.listen(DeadTrack()), 5)
    check("a call with no sound from the phone ends", (await b.receive())["type"] == "websocket.disconnect")
    wa_call.NO_AUDIO_S = keep

    # busy: no room for another call
    await wa_call.handle({"id": "wacid.BUSY", "from": "919000000009", "event": "connect", "session": {"sdp": "v=0"}}, max_calls=0)
    check("too many calls -> rejected", actions == [("reject", None)], actions)
    actions.clear()

    # the caller (Meta's side)
    hospital, question, again, register, cbc = await asyncio.gather(
        speech("मैं टाटा मेमोरियल हॉस्पिटल में हूँ।"), speech("एमआरआई कहाँ होती है?"), speech("ये मुझे मैसेज में मिलेगा?"),
        speech("रजिस्ट्रेशन कहाँ होता है?"), speech("CBC test लेना है, कहाँ जाऊँ?"))
    caller = RTCPeerConnection()
    mouth = wa_call.Speaker()
    caller.addTrack(mouth)
    heard = []                                                 # (time, rms) of what the caller hears

    @caller.on("track")
    def on_track(track):
        async def ear():
            while True:
                try:
                    f = await track.recv()
                except MediaStreamError:
                    return
                x = f.to_ndarray().astype(np.float32)
                heard.append((time.monotonic(), float(np.sqrt(np.mean(x ** 2)))))
        asyncio.create_task(ear())

    await caller.setLocalDescription(await caller.createOffer())
    t0 = time.monotonic()
    await wa_call.handle({"id": "wacid.TEST", "from": "919000000009", "event": "connect",
                          "session": {"sdp_type": "offer", "sdp": caller.localDescription.sdp}})
    kinds = [a for a, _ in actions]
    check("answered with pre_accept then accept", kinds == ["pre_accept", "accept"], kinds)
    check("same SDP answer on both", len(actions) == 2 and actions[0][1] == actions[1][1] and "m=audio" in actions[0][1])
    check("answer is Opus", "opus/48000" in (actions[-1][1] or "").lower())
    print(f"     answered in {time.monotonic() - t0:.1f} s")
    await caller.setRemoteDescription(RTCSessionDescription(sdp=actions[-1][1], type="answer"))

    loud = lambda since: sum(0.02 for t, r in heard if t >= since and r > 300)
    ok = await wait_for(lambda: loud(0) > 1.0, 20)
    check("caller hears the greeting", ok, f"{loud(0):.1f} s of voice")
    greet = [t for r, t in said if r == "bot"]
    print(f"     greeting: {greet[:1]}")
    check("returning patient is greeted by name", any("Ramesh" in t or "रमेश" in t for t in greet), greet)
    last_loud = lambda: max([t for t, r in heard if r > 300] or [0])
    await wait_for(lambda: time.monotonic() - last_loud() > 1.0, 20)   # let the greeting finish

    async def ask(pcm):
        n = len([1 for r, _ in said if r == "bot"])
        mouth.push(b"\0\0" * (48000 // 2) + pcm + b"\0\0" * 48000)
        ok = await wait_for(lambda: len([1 for r, _ in said if r == "bot"]) > n, 25)
        users = [t for r, t in said if r == "user"]
        reply = [t for r, t in said if r == "bot"][-1] if ok else ""
        print(f"     caller said: {users[-1:]}\n     OncoSaathi: {reply}")
        await wait_for(lambda: time.monotonic() - last_loud() > 1.0, 30)   # let the answer play out
        return reply

    reply = await ask(hospital)                                # every new call asks for the hospital first
    check("hospital is understood", "टाटा" in reply or "Tata" in reply, reply)
    t1 = time.monotonic()
    reply = await ask(question)
    check("spoken question gets the MRI building from the booklet", "गोल्डन जुबली" in reply or "GJB" in reply, reply)
    ok = await wait_for(lambda: loud(t1 + 1) > 1.0, 15)
    check("caller hears the answer", ok, f"{loud(t1 + 1):.1f} s of voice")
    check("she says the map is in the WhatsApp chat", "WhatsApp चैट" in reply or "WhatsApp chat" in reply, reply)
    maps = lambda: [u for to, _, u in sent if u and "/map/route.jpg" in u and to == "whatsapp:+919000000009"]
    check("route map sent into the WhatsApp chat", len(maps()) == 1, sent)
    reply = await ask(again)                                   # what the tester asked on the first real call
    check("asking for it as a message sends the map again", len(maps()) == 2, sent)
    check("...and she says so", "फिर से" in reply or "again" in reply, reply)

    # talking over her the way a phone delivers it: quiet, because the phone has already cancelled her echo. She must
    # stop and answer, not take it for her own voice (the second real call lost "मैंने अभी अभी doctor से मिला" this way)
    n = len(said)
    mouth.push(b"\0\0" * (48000 // 2) + register)
    await wait_for(lambda: any(r == "bot" for r, _ in said[n:]), 25)
    await asyncio.sleep(2)                                     # she is now in the middle of her answer
    events.clear()
    mouth.push((np.frombuffer(cbc, np.int16) * 0.12).astype(np.int16).tobytes() + b"\0\0" * 48000)
    ok = await wait_for(lambda: len([1 for r, _ in said[n:] if r == "bot"]) >= 2, 25)
    print(f"     talked over her: {[t for r, t in said[n:] if r == 'user'][1:]}")
    check("caller talking over her is heard, not taken for her echo", ok, said[n:])
    check("...and she stops to listen", "interrupt" in events, events)
    await wait_for(lambda: time.monotonic() - last_loud() > 1.0, 30)

    # caller hangs up
    actions.clear()
    await wa_call.handle({"id": "wacid.TEST", "event": "terminate", "status": "COMPLETED", "duration": 20})
    ok = await wait_for(lambda: "wacid.TEST" not in wa_call.CALLS, 10)
    check("hang-up closes the call", ok)
    check("no terminate sent after the caller hung up", not actions, actions)
    await caller.close()


asyncio.run(main())
print("\nALL PASSED" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
