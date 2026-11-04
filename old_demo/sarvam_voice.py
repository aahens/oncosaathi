"""Sarvam calls used by OncoSaathi: Saaras (ears), Bulbul (mouth), and an optional
label-only intent classifier."""
import base64
import os
from functools import lru_cache
from pathlib import Path

from sarvamai import SarvamAI


def load_env() -> None:
    """Load KEY=value lines from .env next to this file into os.environ (no extra packages)."""
    env_file = Path(__file__).with_name(".env")
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env()
if not os.environ.get("SARVAM_API_KEY"):
    raise SystemExit("SARVAM_API_KEY is empty. Paste your key into .env and run again.")

client = SarvamAI(api_subscription_key=os.environ["SARVAM_API_KEY"])

STT_MODEL = os.environ.get("SARVAM_STT_MODEL", "saaras:v4")
TTS_MODEL = os.environ.get("SARVAM_TTS_MODEL", "bulbul:v3")
TTS_SPEAKER = os.environ.get("SARVAM_TTS_SPEAKER", "priya")
TTS_PACE = float(os.environ.get("SARVAM_TTS_PACE", "0.9"))
CHAT_MODEL = os.environ.get("SARVAM_CHAT_MODEL", "sarvam-105b-conversations")
TRANSLATE_MODEL = os.environ.get("SARVAM_TRANSLATE_MODEL", "sarvam-translate:v1")

# Languages Bulbul can speak. Saaras hears more than this, so we fall back to Hindi.
TTS_LANGS = {"hi-IN", "mr-IN", "bn-IN", "ta-IN", "te-IN", "kn-IN", "ml-IN", "gu-IN", "pa-IN", "od-IN", "en-IN"}

# Hints so Saaras catches hospital words.
KEYTERMS = ["CBC", "chemo", "chemotherapy", "daycare", "PET-CT", "CT scan", "MRI", "OPD",
            "token", "pharmacy", "registration", "report", "Ayushman Bharat"]


def speech_to_text(audio: bytes, filename: str = "voice.ogg") -> dict:
    """Voice note bytes (ogg/opus, webm, wav, mp3...) -> {transcript, language_code, language_probability}."""
    r = client.speech_to_text.transcribe(
        file=(filename, audio),
        model=STT_MODEL,
        mode="codemix",
        language_code="unknown",
        keyterms=KEYTERMS,
    )
    return {
        "transcript": (r.transcript or "").strip(),
        "language_code": r.language_code or "hi-IN",
        "language_probability": getattr(r, "language_probability", None),
    }


@lru_cache(maxsize=256)  # replies are fixed sentences, so each one is synthesised only once
def text_to_speech(text: str, lang: str) -> bytes:
    """Text -> OGG/Opus bytes, the format WhatsApp voice notes use (no ffmpeg needed)."""
    r = client.text_to_speech.convert(
        text=text,
        language_code=lang if lang in TTS_LANGS else "hi-IN",
        model=TTS_MODEL,
        speaker=TTS_SPEAKER,
        pace=TTS_PACE,
        output_audio_codec="opus",
        speech_sample_rate=24000,  # opus rejects Bulbul's default 22050 Hz
    )
    return base64.b64decode("".join(r.audios))


def detect_language(text: str) -> str:
    """Language code of typed text, e.g. 'ta-IN'. Falls back to Hindi."""
    r = client.text.identify_language(input=text)
    return r.language_code or "hi-IN"


@lru_cache(maxsize=1024)
def translate(text: str, source: str, target: str) -> str:
    """Translate with Sarvam. Cached, so each fixed reply is translated once per language."""
    r = client.text.translate(
        input=text,
        source_language_code=source,
        target_language_code=target,
        model=TRANSLATE_MODEL,
    )
    return (r.translated_text or "").strip()


def classify(text: str) -> str:
    """Ask the Sarvam LLM to pick ONE label. It never writes the reply itself."""
    from router import LLM_LABELS

    resp = client.chat.completions(
        model=CHAT_MODEL,
        temperature=0,
        max_tokens=20,
        messages=[
            {"role": "system", "content": (
                "You label messages sent to a cancer hospital's navigation helpdesk. "
                "Reply with exactly one label and nothing else, from: " + ", ".join(LLM_LABELS) + ". "
                "Use 'clinical' for anything about symptoms, medicines, doses, treatment or test results. "
                "Use 'emergency' for breathing trouble, heavy bleeding, fainting or chest pain. "
                "Use 'unknown' if it is not about one of the listed places.")},
            {"role": "user", "content": text},
        ],
    )
    out = (resp.choices[0].message.content or "").strip().lower()
    for label in sorted(LLM_LABELS, key=len, reverse=True):
        if label in out:
            return label
    return "unknown"
