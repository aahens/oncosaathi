"""Real Sarvam translation and voice for the languages we show. Run: python language_test.py (uses a temporary database)."""
import asyncio
import os
import re
import tempfile

os.environ["ONCO_DB"] = os.path.join(tempfile.mkdtemp(), "lang.db")
import brain  # noqa: E402
import core  # noqa: E402

fails = 0
SCRIPT = {"ta": r"[஀-௿]", "kn": r"[ಀ-೿]", "hi": r"[ऀ-ॿ]", "en": r"[A-Za-z]"}


def check(name, ok, detail=""):
    global fails
    fails += not ok
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  -> {detail}"))


def state():
    return {"pid": None, "mode": "chat", "awaiting": None, "where": None, "place": None, "hospital": "tata-parel"}


async def main():
    for lang, q in [("en", "Where is the pharmacy?"), ("hi", "फ़ार्मेसी कहाँ है?"), ("ta", "பார்மசி எங்கே உள்ளது?"), ("kn", "ಫಾರ್ಮಸಿ ಎಲ್ಲಿದೆ?")]:
        o = await brain.respond(q, state())
        check(f"{lang}: answered in that language", o["lang"] == lang and re.search(SCRIPT[lang], o["text"]), o["text"][:80])
        check(f"{lang}: counter number kept", "54" in o["text"], o["text"][:160])
        check(f"{lang}: map included", bool(o.get("map")), o.get("route"))
        path, b64 = core.tts(o["text"], o["lang"], "opus")
        os.remove(path)
        check(f"{lang}: voice note made", len(b64) > 1000)
    o = await brain.respond("பார்மசி எங்கே உள்ளது?", state())
    check("ta: English department name kept", o["text"].startswith("Dispensary"), o["text"][:60])

    st = state()
    o = await brain.respond("speak in tamil, i dont understand hindi", st)
    check("'speak in tamil' answered in Tamil", o["lang"] == "ta" and re.search(SCRIPT["ta"], o["text"]), o["text"])
    o = await brain.respond("Where is endoscopy?", st)
    check("English question stays in Tamil afterwards", o["lang"] == "ta" and re.search(SCRIPT["ta"], o["text"]) and o.get("map"), o["text"][:80])
    o = await brain.respond("I have chest pain", st)
    check("safety still works after switching", "EMERGENCY" in o["route"] and o["lang"] == "ta" and "map" not in o, o["route"])
    o = await brain.respond("ಎಂಡೋಸ್ಕೋಪಿ ಎಲ್ಲಿದೆ?", st)
    check("writing Kannada moves to Kannada", o["lang"] == "kn" and "lang_pref" not in st, o["lang"])
    o = await brain.respond("speak in english", st)
    check("'speak in english' answered in English", o["lang"] == "en", o["text"])
    o = await brain.respond("Endoscopy kahan hai?", st)
    check("Hinglish then stays in English as asked", o["lang"] == "en", o["lang"])
    o = await brain.respond("kannada mein batao, endoscopy kahan hai", state())
    check("language and question together get answered", o["lang"] == "kn" and o.get("map"), o["route"])


asyncio.run(main())
print("\nALL PASSED" if not fails else f"\n{fails} FAILED")
raise SystemExit(1 if fails else 0)
