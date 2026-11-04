"""Live call, run by us: browser mic -> Sarvam streaming STT (saaras:v4) -> brain.py -> Sarvam TTS (bulbul) -> browser.

Speech-to-text is streamed: every 100 ms mic frame goes straight to Sarvam's WebSocket, and Sarvam returns the
transcript ~0.6 s after the caller stops talking (batch uploads took 3-13 s on 21 Oct 2026). Our own energy VAD is
still used for barge-in and to ignore the bot's own voice coming back through the mic. If the stream can't open or
drops, the call falls back to the old way: our VAD cuts the utterance and it is uploaded to the REST endpoint.

Every reply comes from brain.py, so every direction comes from hospital.json. The browser protocol is the same as the old
Sarvam-agent proxy (connected / transcript / audio / event:interrupt / error / ended), so static/index.html is unchanged.
Input: 16 kHz mono PCM16 frames over the WebSocket. Output: base64 PCM16 chunks with their sample rate.
"""
import asyncio
import base64
import collections
import io
import json
import re
import time
import wave

import httpx
import numpy as np
from fastapi import WebSocket
from sarvamai import AsyncSarvamAI

import brain
import core
import patient_store as store

SR = 16000
SUB = 320                    # 20 ms analysis frames
START_MS = 200               # voiced audio needed to start an utterance
END_SILENCE_MS = 750         # silence that ends it
MIN_UTT_MS = 350             # shorter blips are ignored
MAX_UTT_MS = 15000
voice_call_end_ms = END_SILENCE_MS
PREROLL_FRAMES = 15          # 300 ms kept from before the start, so first syllables aren't clipped
STT_HOTWORDS = list(dict.fromkeys(core.KEYTERMS + ["LFT", "KFT", "histopathology", "USG", "X-ray", "RT card", "Day Care", "endoscopy",
                                "PET-CT", "mammography", "OncoSaathi"]))


class VAD:
    """Energy VAD with an adaptive noise floor. Stricter while the bot is talking, so echo doesn't count as barge-in."""

    def __init__(self):
        self.floor = 200.0
        self.active = False
        self.voiced = 0
        self.silence = 0
        self.frames = []
        self.pre = collections.deque(maxlen=PREROLL_FRAMES)
        self.rest = b""

    def feed(self, pcm, bot_talking):
        """Yield ("start", None) and ("end", utterance_bytes) events."""
        data = self.rest + pcm
        n = len(data) // (SUB * 2) * SUB * 2
        self.rest = data[n:]
        for i in range(0, n, SUB * 2):
            fr = data[i:i + SUB * 2]
            rms = float(np.sqrt(np.mean(np.frombuffer(fr, np.int16).astype(np.float32) ** 2)))
            thr = max(self.floor * (5.0 if bot_talking else 3.0), 1200.0 if bot_talking else 450.0)
            loud = rms > thr
            if not self.active:
                self.pre.append(fr)
                self.voiced = self.voiced + 20 if loud else max(0, self.voiced - 10)
                if not loud:
                    self.floor = 0.97 * self.floor + 0.03 * max(rms, 50.0)
                if self.voiced >= START_MS:
                    self.active, self.silence = True, 0
                    self.frames = list(self.pre)
                    yield "start", None
                continue
            self.frames.append(fr)
            self.silence = 0 if loud else self.silence + 20
            dur = len(self.frames) * 20
            if self.silence >= END_SILENCE_MS or dur >= MAX_UTT_MS:
                utt = b"".join(self.frames)
                self.active, self.voiced, self.frames = False, 0, []
                self.pre.clear()
                if dur - self.silence >= MIN_UTT_MS:
                    yield "end", utt


def to_wav(pcm, sr=SR):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return buf.getvalue()


class Call:
    ECHO = True                    # the browser's speaker can leak into its mic; a phone call cancels its own echo

    def __init__(self, ws: WebSocket, token: str):
        self.ws = ws
        self.uhid = store.patient_for_session(token) or ""          # patient id (log label only)
        self.state = {"pid": self.uhid or None, "mode": "voice", "awaiting": None, "where": None, "place": None}
        self.http = httpx.AsyncClient(timeout=30, headers={"api-subscription-key": core.SARVAM_KEY})
        self.vad = VAD()
        self.turn = None               # task handling the current utterance
        self.turn_audio = b""          # its audio, kept so a quick continuation can be merged into it
        self.talking_until = 0.0       # when the bot's queued audio finishes playing in the browser
        self.committed = False         # current turn has a decided reply (no more merging)
        self.closed = False
        self.first_audio_at = 0.0
        self.stream = None             # Sarvam streaming STT socket (None -> REST fallback)
        self._stream_cm = None
        self._stream_reader = None
        self.last_vad_start = 0.0      # when our VAD last heard the caller start talking
        self.bot_started = 0.0         # when the bot last started speaking
        self.pending_text = ""         # transcript of a turn still being thought about (merged with a quick follow-on)

    async def send(self, obj):
        if self.closed:
            return
        try:
            await self.ws.send_json(obj)
        except Exception:
            self.closed = True

    # ---- Sarvam streaming STT ----
    async def open_stream(self):
        try:
            client = AsyncSarvamAI(api_subscription_key=core.SARVAM_KEY)
            self._stream_cm = client.speech_to_text_streaming.connect(
                language_code="unknown", model=core.STT_MODEL, mode="codemix", sample_rate=str(SR),
                input_audio_codec="pcm_s16le", vad_signals="true", flush_signal="true", keyterms=json.dumps(STT_HOTWORDS))
            self.stream = await asyncio.wait_for(self._stream_cm.__aenter__(), 8)
            self._stream_reader = asyncio.create_task(self.read_stream())
            print("[call] streaming STT connected", flush=True)
        except Exception as e:
            print(f"[call] streaming STT unavailable, using uploads: {e!r}", flush=True)
            self.stream = None

    async def read_stream(self):
        try:
            async for m in self.stream:
                if m.type == "data" and getattr(m.data, "transcript", None):
                    await self.on_transcript(m.data.transcript.strip(), getattr(m.data, "language_code", "") or "")
                elif m.type == "error":
                    print(f"[call] streaming STT error: {m.data}", flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[call] streaming STT dropped, falling back to uploads: {e!r}", flush=True)
        self.stream = None

    async def feed_stream(self, pcm):
        try:
            await self.stream.transcribe(audio=base64.b64encode(pcm).decode(), sample_rate=SR)
        except Exception as e:
            print(f"[call] streaming STT send failed, falling back to uploads: {e!r}", flush=True)
            self.stream = None

    async def on_transcript(self, text, stt_lang):
        if not text or len(text.strip(" .।?")) < 2:
            return
        now = time.monotonic()
        if self.bot_started > self.last_vad_start and now < self.talking_until + 1.0:
            if self.ECHO:
                print(f"[call] ignored (bot's own voice): {text!r}", flush=True)   # echo of the bot, not the caller
                return
            await self.barge_in()                              # no echo here: it's the caller, too quiet for our VAD
        if self.turn and not self.turn.done() and not self.committed:
            self.turn.cancel()                                 # a quick follow-on: answer both parts together
            text = f"{self.pending_text} {text}".strip()
        self.pending_text = text
        self.committed = False
        self.turn = asyncio.create_task(self.handle_text(text, stt_lang, time.perf_counter(), 0))

    # ---- Sarvam calls ----
    async def stt(self, pcm):
        r = await self.http.post(f"{core.SARVAM}/speech-to-text",
                                 files={"file": ("turn.wav", to_wav(pcm), "audio/wav")},
                                 data={"model": core.STT_MODEL, "language_code": "unknown", "mode": "codemix",
                                       "keyterms": json.dumps(STT_HOTWORDS)})
        r.raise_for_status()
        j = r.json()
        return (j.get("transcript") or "").strip(), j.get("language_code") or ""

    async def tts(self, text, lang):
        r = await self.http.post(f"{core.SARVAM}/text-to-speech",
                                 json={"text": text[:1400], "target_language_code": core.LANG_CODE.get(lang, "hi-IN"),
                                       "model": core.TTS_MODEL, "speaker": core.voice_for(lang),
                                       "speech_sample_rate": SR, "enable_preprocessing": True})
        r.raise_for_status()
        j = r.json()
        wav = base64.b64decode((j.get("audios") or j.get("audio"))[0])
        with wave.open(io.BytesIO(wav)) as w:
            return w.readframes(w.getnframes()), w.getframerate()

    async def speak(self, text, lang):
        # Synthesize sentences in parallel and send them in order, so the first one plays while the rest are made.
        parts = [p for p in re.split(r"(?<=[।.?!])\s+", text) if p.strip()] or [text]
        jobs = [asyncio.create_task(self.tts(p, lang)) for p in parts]
        try:
            for k, job in enumerate(jobs):
                pcm, sr = await job
                if k == 0:
                    self.first_audio_at = time.perf_counter()
                    self.bot_started = time.monotonic()
                    await self.send({"type": "transcript", "role": "bot", "text": text})   # caption appears with the audio
                self.talking_until = max(time.monotonic(), self.talking_until) + len(pcm) / 2 / sr
                step = sr // 2                                 # ~0.5 s chunks so an interrupt cuts in quickly
                for i in range(0, len(pcm), step * 2):
                    await self.send({"type": "audio", "b64": base64.b64encode(pcm[i:i + step * 2]).decode(), "sr": sr})
        finally:
            for job in jobs:
                job.cancel()

    # ---- turns ----
    async def handle(self, pcm):
        """Fallback path: upload the utterance our VAD cut, then answer it."""
        t0 = time.perf_counter()
        try:
            text, stt_lang = await self.stt(pcm)
        except Exception as e:
            print(f"[call] STT failed: {e!r}", flush=True)
            return
        if not text or len(text.strip(" .।?")) < 2:
            return
        await self.handle_text(text, stt_lang, t0, int((time.perf_counter() - t0) * 1000))

    async def respond(self, text, stt_lang):
        """One answer from brain.py. wa_call.WhatsAppCall adds what only a WhatsApp call needs."""
        return await brain.respond(text, self.state, stt_lang)

    async def handle_text(self, text, stt_lang, t0, stt_ms):
        t1 = time.perf_counter()
        out = await self.respond(text, stt_lang)
        reply, lang, route = out["text"], out["lang"], out["route"]
        self.committed = True                                  # from here on, new speech is a barge-in, not a continuation
        self.turn_audio = b""
        self.pending_text = ""
        await self.send({"type": "transcript", "role": "user", "text": text})
        if out.get("map"):
            await self.send({"type": "map", **out["map"]})     # the call screen shows the walkthrough map while she speaks
        t2 = time.perf_counter()
        try:
            await self.speak(reply, lang)
        except Exception as e:
            print(f"[call] TTS failed: {e!r}", flush=True)
            return
        how = f"upload stt {stt_ms} ms" if stt_ms else "streaming stt"
        print(f"[call] patient={self.uhid or '-'} | {text!r} -> {route} | {how}, brain {int((t2-t1)*1000)} ms, "
              f"tts first audio {int((self.first_audio_at-t2)*1000)} ms => reply starts {int((self.first_audio_at-t0)*1000)} ms "
              f"after the transcript", flush=True)

    async def on_speech_start(self):
        self.last_vad_start = time.monotonic()
        pending = self.turn is not None and not self.turn.done()
        if time.monotonic() < self.talking_until or (pending and self.committed):
            await self.barge_in()
        elif pending and self.stream is None:
            # upload mode: caller paused mid-sentence while we were still transcribing: merge it with what comes next
            self.turn.cancel()
        elif self.stream is not None:
            pass                                              # streaming mode merges in on_transcript
        else:
            self.turn_audio = b""

    async def barge_in(self):
        """The caller talks while the bot is answering: stop its audio and drop the rest of that answer."""
        self.talking_until = 0.0
        if self.turn is not None and not self.turn.done():
            self.turn.cancel()
        self.turn_audio = b""
        await self.send({"type": "event", "name": "interrupt"})

    def on_speech_end(self, pcm):
        self.turn_audio += pcm
        self.committed = False
        self.turn = asyncio.create_task(self.handle(self.turn_audio))

    async def run(self):
        await self.send({"type": "connected"})
        print(f"Call connected (local pipeline): patient={self.uhid or '-'}", flush=True)
        await asyncio.gather(self.open_stream(), self.speak(brain.opening(self.state), "hi"))
        while not self.closed:
            m = await self.ws.receive()
            if m.get("type") == "websocket.disconnect":
                break
            if not m.get("bytes"):
                continue
            if self.stream is not None:
                await self.feed_stream(m["bytes"])
            for ev, utt in list(self.vad.feed(m["bytes"], time.monotonic() < self.talking_until)):
                if ev == "start":
                    await self.on_speech_start()
                elif self.stream is None:                     # upload fallback: our VAD decides the turn
                    self.on_speech_end(utt)

    async def close(self):
        self.closed = True
        if self.turn and not self.turn.done():
            self.turn.cancel()
        if self._stream_reader:
            self._stream_reader.cancel()
        if self._stream_cm:
            try:
                await self._stream_cm.__aexit__(None, None, None)
            except Exception:
                pass
        await self.http.aclose()


async def run_call(ws: WebSocket, token: str):
    call = Call(ws, token)
    try:
        await call.run()
    except Exception as e:
        if "disconnect" not in type(e).__name__.lower():
            print("Local call error:", repr(e), flush=True)
            await call.send({"type": "error", "message": f"Call error: {e}"})
    finally:
        await call.send({"type": "ended"})
        await call.close()
