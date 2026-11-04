"""WhatsApp voice call: the patient taps 📞 in WhatsApp and talks to OncoSaathi, answered the same way as the web call.

    phone -> Meta -> POST /whatsapp/meta, field "calls", event "connect" with an SDP offer
      -> aiortc answers it (WebRTC: ICE + DTLS-SRTP, Opus) -> pre_accept + accept with our SDP answer
      -> caller audio (48 kHz Opus) -> 16 kHz PCM -> who answers depends on CALL_ENGINE, as on the web page:
           sarvam_agent: the hosted Sarvam agent (samvaad_call.py); it listens, takes turns and handles barge-in itself
           local: our pipeline (voice_call.Call: streaming STT -> brain.py -> TTS); a walkthrough map goes into the
                  WhatsApp chat as a photo, she says so, and "message mein bhej do" sends the last map again
      -> bot audio -> 48 kHz frames back to the caller.
    event "terminate" (caller hung up) or our time limit -> the call is closed.

Both are written for the browser's WebSocket, so Bridge looks like one to them. Calling must be switched on for
the number first (python wa_setup.py), and the app must be subscribed to the "calls" webhook field.
"""
import asyncio
import base64
import fractions
import re
import secrets
import time

import av
import numpy as np
from aiortc import MediaStreamTrack, RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError

import brain
import core
import patient_store as store
import samvaad_call
import voice_call
import whatsapp

OUT_SR = 48000
FRAME = 960                    # 20 ms at 48 kHz
PREBUFFER_MS = 300             # voice held before a reply starts playing (see Speaker)
PREBUFFER = OUT_SR * PREBUFFER_MS // 1000 * 2
LOW = PREBUFFER * 2 // 3       # below this (200 ms) a pause is stretched so the store refills
QUIET = 200                    # mean level of a 20 ms frame below which it counts as a pause between words
STUN = [RTCIceServer(urls="stun:stun.l.google.com:19302")]
CALLS = {}                     # WhatsApp call id -> Session
NO_AUDIO_S = 30                # a phone sends sound (or comfort noise) all the time; none this long means the call is dead
                               # (the laptop slept mid-call on 3 Nov 2026 and the dead call was held until the time limit)
_SENDS = set()                 # maps on their way to the chat; not cancelled when the call ends
# "ye mujhe message mein milega?", "can you send me a message of where to go?" (heard on a real call, 3 Nov 2026)
ASK_IN_CHAT = (r"\b(message|messages|msg|sms|whatsapp|chat|text me|in writing|likh ?(ke|kar))\b"
               r"|मैसेज|मेसेज|एसएमएस|व्हाट्सएप|व्हाट्सऐप|वॉट्सऐप|चैट|लिखकर|लिख कर")
IN_CHAT = {"hi": "रास्ते का नक्शा आपके WhatsApp चैट में भेज दिया है।", "en": "I've sent the route map to your WhatsApp chat."}
RESENT = {"hi": "जी, रास्ते का नक्शा फिर से आपके WhatsApp चैट में भेज दिया है।",
          "en": "Yes, I've sent the route map to your WhatsApp chat again."}
NO_MAP = {"hi": "जी, बताइए कहाँ जाना है, मैं रास्ता आपके WhatsApp चैट में भेज दूँगी।",
          "en": "Sure. Tell me where you want to go, and I'll send the route to your WhatsApp chat."}
# after a call with the hosted agent (it can't send anything into the chat): the routes asked about, from our data
AFTER_CALL = {"hi": "📞 आपकी कॉल में पूछे गए रास्ते, लिखकर और नक्शे पर:", "en": "📞 The routes from your call, in writing and on the map:"}
MAX_ROUTES = 3


class Speaker(MediaStreamTrack):
    """What the caller hears: queued bot audio in real time, silence in between.

    The hosted agent streams its voice in 0.1 s pieces at speaking speed, and a piece that arrives a little late used to
    become a gap in the middle of a word (4 Nov 2026). So each reply is held PREBUFFER_MS before it starts playing, and
    while it plays, a low store is refilled by stretching a pause between words by 20 ms, never by cutting a word."""
    kind = "audio"

    def __init__(self):
        super().__init__()
        self.buf = bytearray()
        self.pts = 0
        self.t0 = None
        self.playing = False           # False: collecting PREBUFFER_MS of voice before speaking
        self.held = 0                  # 20 ms frames spent collecting
        self.dry_at = None             # when the voice last ran out in the middle of a sound
        self.gaps = 0                  # ...and more came within 0.5 s: a word cut short, which the caller heard
        self.last_push = 0.0
        self.stretched = 0             # 20 ms frames added to the current pause

    def push(self, pcm48):
        now = time.monotonic()
        if self.dry_at is not None and now - self.dry_at < 0.5:
            self.gaps += 1
            print(f"[wa-call] a word was cut short: the next voice came {now - self.dry_at:.2f} s late", flush=True)
        self.dry_at, self.last_push = None, now
        self.buf += pcm48

    def clear(self):
        self.buf.clear()
        self.playing, self.held, self.dry_at, self.stretched = False, 0, None, 0

    async def recv(self):
        if self.t0 is None:
            self.t0 = time.monotonic()
        wait = self.t0 + self.pts / OUT_SR - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        if not self.playing and self.buf:
            self.held += 1
            self.playing = len(self.buf) >= PREBUFFER or self.held * 20 >= PREBUFFER_MS
        chunk = b""
        if self.playing:
            nxt = bytes(self.buf[:FRAME * 2])
            pause = len(nxt) == FRAME * 2 and np.abs(np.frombuffer(nxt, np.int16)).mean() < QUIET
            flowing = time.monotonic() - self.last_push < 0.15         # more voice is still on its way
            if pause and len(self.buf) < LOW and flowing and self.stretched < 5:
                self.stretched += 1                            # low and between words: 20 ms more pause, at most 100 ms
            else:
                chunk, self.stretched = nxt, 0
                del self.buf[:FRAME * 2]
            if not self.buf:                                   # ran out; it only matters if it stopped mid-sound
                voiced = bool(chunk) and np.abs(np.frombuffer(chunk, np.int16)).mean() >= QUIET
                self.playing, self.held, self.dry_at = False, 0, time.monotonic() if voiced else None
        chunk = chunk.ljust(FRAME * 2, b"\0")
        frame = av.AudioFrame(format="s16", layout="mono", samples=FRAME)
        frame.planes[0].update(chunk)
        frame.sample_rate, frame.pts, frame.time_base = OUT_SR, self.pts, fractions.Fraction(1, OUT_SR)
        self.pts += FRAME
        return frame


class Bridge:
    """Stands in for the browser WebSocket that voice_call.Call talks to."""

    def __init__(self, to):
        self.to = to                   # "whatsapp:+91…", for sending the map into the chat
        self.speaker = Speaker()
        self.inbox = asyncio.Queue()   # 16 kHz PCM from the caller; None when the call is over
        self.maps = set()
        self.tasks = set()
        self.up, self.up_sr = None, None   # one resampler per call, so 0.1 s pieces join without a click
        self.heard = []                # the caller's words (captions), for the routes sent after an agent call

    def to_speaker(self, pcm, sr):
        """Bot audio (PCM16 at any rate) -> 48 kHz for the phone."""
        if not pcm:
            return
        if sr != self.up_sr:
            self.up, self.up_sr = av.AudioResampler(format="s16", layout="mono", rate=OUT_SR), sr
        frame = av.AudioFrame.from_ndarray(np.frombuffer(pcm, np.int16).reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = sr
        for f in self.up.resample(frame):
            self.speaker.push(bytes(f.planes[0])[:f.samples * 2])

    async def receive(self):
        buf = b""
        while len(buf) < 3200:         # 100 ms per message, like the browser
            chunk = await self.inbox.get()
            if chunk is None:
                self.inbox.put_nowait(None)
                return {"type": "websocket.disconnect"}
            buf += chunk
        return {"type": "websocket.receive", "bytes": buf}

    async def send_json(self, m):
        kind = m.get("type")
        if kind == "transcript" and "user" in str(m.get("role", "")).lower() and m.get("text"):
            self.heard.append(m["text"])
        elif kind == "audio":
            self.to_speaker(base64.b64decode(m["b64"]), m.get("sr") or voice_call.SR)
        elif kind == "event" and "interrupt" in m.get("name", "").lower():   # ours: "interrupt"; the agent's: "…user_interrupt"
            self.speaker.clear()                               # the caller started talking: stop the bot at once
        elif kind == "map" and m.get("url") not in self.maps and whatsapp.public_base():
            self.maps.add(m["url"])
            task = asyncio.create_task(self.send_map(m))
            _SENDS.add(task)
            task.add_done_callback(_SENDS.discard)

    async def send_map(self, m):
        r = await whatsapp.send(self.to, body=m.get("caption", ""), media_url=whatsapp.public_base() + m["url"])
        print(f"[wa-call] -> {whatsapp.mask(self.to)}: map {r[:300]}", flush=True)

    async def listen(self, track):
        resampler = av.AudioResampler(format="s16", layout="mono", rate=voice_call.SR)
        while True:
            try:
                frame = await asyncio.wait_for(track.recv(), NO_AUDIO_S)
            except MediaStreamError:
                break
            except asyncio.TimeoutError:
                print(f"[wa-call] no audio from {whatsapp.mask(self.to)} for {NO_AUDIO_S} s: ending the call", flush=True)
                break
            for f in resampler.resample(frame):
                self.inbox.put_nowait(bytes(f.planes[0])[:f.samples * 2])
        self.inbox.put_nowait(None)


class WhatsAppCall(voice_call.Call):
    """The web call shows the map on screen; on a phone call it goes into the WhatsApp chat instead, so she says so,
    and asking for it as a message sends the last map again."""
    ECHO = False                                               # WhatsApp cancels the echo on the phone

    def __init__(self, bridge, token):
        super().__init__(bridge, token)
        self.last_map = None

    async def respond(self, text, stt_lang):
        low = " " + text.lower() + " "
        unsafe = any(w in low for w in core.SELF_HARM + core.EMERGENCY + core.CLINICAL + core.EMOTIONAL)
        if re.search(ASK_IN_CHAT, low) and not unsafe:
            lang = self.state.get("lang_pref") or core.pick_lang(text, "Auto", stt_lang)
            out = {"lang": lang, "route": "SEND_TO_CHAT via rules-call", "actions": []}
            if self.last_map:
                self.ws.maps.discard(self.last_map["url"])     # they asked for it: send it again
                out.update(text=await self.line(RESENT, lang), map=self.last_map)
            else:
                out["text"] = await self.line(NO_MAP, lang)
            return out
        out = await super().respond(text, stt_lang)
        if out.get("map"):
            self.last_map = out["map"]
            out["text"] += " " + await self.line(IN_CHAT, out["lang"])
        return out

    async def line(self, words, lang):
        if lang in words:
            return words[lang]
        return await brain.translate(words["en"], "en-IN", core.LANG_CODE.get(lang, "hi-IN")) or words["hi"]


class Session:
    def __init__(self, call_id, caller):
        self.id = call_id
        self.caller = caller                                   # digits, e.g. "919876543210"
        self.bridge = Bridge(f"whatsapp:+{caller}")
        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=STUN))
        self.hung_up = False
        self.task = None

    async def answer(self, offer):
        """Our SDP answer for Meta's offer (ICE candidates included, so there is no trickle)."""
        @self.pc.on("track")
        def on_track(track):
            if track.kind == "audio":
                self.bridge.tasks.add(asyncio.create_task(self.bridge.listen(track)))

        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=offer, type="offer"))
        self.pc.addTrack(self.bridge.speaker)
        await self.pc.setLocalDescription(await self.pc.createAnswer())
        return self.pc.localDescription.sdp

    def token(self):
        """A session for a patient who has already agreed on WhatsApp, so the call knows their name and next step."""
        p = store.find_by_phone(self.caller, actor="whatsapp-call")
        return store.new_session(p["id"], "whatsapp-call") if p and p.get("consent_at") else ""

    async def run(self, minutes, engine="local"):
        token, call, st = self.token(), None, None
        if engine == "sarvam_agent" and samvaad_call.ready():
            # the same hosted agent as the web page's 📞; someone who hasn't agreed yet gets a throwaway caller id
            pid = store.patient_for_session(token) if token else None
            st = {"pid": pid, "mode": "voice", "awaiting": None, "where": None, "place": None}
            job = samvaad_call.run(self.bridge, token or f"wa-{secrets.token_hex(8)}", st)
        else:
            call = WhatsAppCall(self.bridge, token)
            job = call.run()
        try:
            await asyncio.wait_for(job, minutes * 60)
        except asyncio.TimeoutError:
            print("  [wa-call] ended at the time limit", flush=True)
        except Exception as e:
            print(f"[wa-call] error: {e!r}", flush=True)
        finally:
            if call:
                await call.close()
            await self.close()
            if st is not None and self.bridge.heard:           # an agent call: the routes into the chat, after it
                task = asyncio.create_task(self.send_routes(st))
                _SENDS.add(task)
                task.add_done_callback(_SENDS.discard)

    async def send_routes(self, st):
        """Each place the caller asked about, as the written route and the map with arrows (the same answer the WhatsApp
        chat gives, from hospital.json), since the hosted agent can't send anything into the chat (asked for, 4 Nov 2026)."""
        hosp = st.get("hospital")
        if not hosp and st.get("pid"):
            hosp = (store.get(st["pid"], actor="whatsapp-call") or {}).get("hospital")
        if hosp != "tata-parel" or not whatsapp.public_base():
            return
        state = {"pid": None, "mode": "chat", "awaiting": None, "where": None, "place": None, "hospital": hosp}
        routes, lang = {}, "hi"
        for text in self.bridge.heard:                         # in order, so "wahan kaise jaun" follows the place before it
            try:
                out = await brain.respond(text[:500], state)
            except Exception as e:
                print(f"[wa-call] route summary: {e!r}", flush=True)
                continue
            if out.get("map"):
                routes[out["map"]["to"]] = out
                lang = out["lang"]
        if not routes:
            return
        to = self.bridge.to
        head = AFTER_CALL.get(lang) or await brain.translate(AFTER_CALL["en"], "en-IN", core.LANG_CODE.get(lang, "hi-IN")) or AFTER_CALL["hi"]
        await whatsapp.send(to, body=head)
        for dest, out in list(routes.items())[-MAX_ROUTES:]:
            name = brain.place_name(dest, out["lang"])
            text = out["text"] if out["text"].startswith(name) else f"*{name}*: {out['text']}"
            r1 = await whatsapp.send(to, body=text)
            r2 = await whatsapp.send(to, body=out["map"].get("caption", ""), media_url=whatsapp.public_base() + out["map"]["url"])
            print(f"[wa-call] -> {whatsapp.mask(to)}: route to {dest} after the call: text {r1[:40]}, map {r2[:40]}", flush=True)

    async def close(self):
        if CALLS.pop(self.id, None) is None:
            return
        if not self.hung_up:
            ok, detail = await whatsapp.call_action(self.id, "terminate")
            print(f"[wa-call] terminate: {'ok' if ok else detail}", flush=True)
        self.bridge.inbox.put_nowait(None)
        for t in list(self.bridge.tasks):
            t.cancel()
        print(f"[wa-call] words cut short by late voice: {self.bridge.speaker.gaps}", flush=True)
        await self.pc.close()


async def handle(event, max_calls=3, minutes=10, engine="local"):
    """One "calls" webhook event from Meta. engine: server.CALL_ENGINE, so WhatsApp calls match the web page's 📞."""
    cid, kind = event.get("id", ""), event.get("event")
    if kind == "connect" and cid not in CALLS:
        if len(CALLS) >= max_calls:
            await whatsapp.call_action(cid, "reject")
            print("[wa-call] rejected: too many calls at once", flush=True)
            return
        s = CALLS[cid] = Session(cid, event.get("from", ""))
        try:
            sdp = await s.answer(event["session"]["sdp"])
            for action in ("pre_accept", "accept"):            # pre_accept lets media start early, so the greeting isn't clipped
                ok, detail = await whatsapp.call_action(cid, action, sdp)
                if not ok:
                    raise RuntimeError(f"{action} failed: {detail}")
        except Exception as e:
            print(f"[wa-call] could not answer {whatsapp.mask(s.caller)}: {e!r}", flush=True)
            s.hung_up = True                                   # nothing to terminate on Meta's side
            await s.close()
            await whatsapp.call_action(cid, "reject")
            return
        print(f"[wa-call] answered {whatsapp.mask(s.caller)}", flush=True)
        s.task = asyncio.create_task(s.run(minutes, engine))
    elif kind == "terminate":
        s = CALLS.get(cid)
        if s:
            s.hung_up = True
            s.bridge.inbox.put_nowait(None)                    # Call.run sees a disconnect and finishes
        print(f"[wa-call] ended by caller ({event.get('status', '')}, {event.get('duration', 0)} s)", flush=True)
