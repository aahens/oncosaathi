"""Walkthrough map checks, no Sarvam calls. Run: python route_test.py"""
import asyncio
import io
import os
import tempfile

os.environ["ONCO_DB"] = os.path.join(tempfile.mkdtemp(), "route.db")
from PIL import Image  # noqa: E402

import brain  # noqa: E402
import make_floor_plans as plans  # noqa: E402
import route_map as rm  # noqa: E402

fails = 0


def check(name, ok, detail=""):
    global fails
    fails += not ok
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"  -> {detail}"))


# 1. every route between every pair of places stays on the corridor and ends at the right door
places = [p["id"] for p in rm.start_choices()]
bad = []
for a in places:
    for b in places:
        if a == b:
            continue
        trip = rm.legs(a, b)
        for leg in trip:
            mid = leg["points"][1:-1]
            if any(y != rm.CORRIDOR_Y for _, y in mid):
                bad.append((a, b, "left the corridor"))
            if leg["points"][-1] != rm.door(leg["to"])[0]:
                bad.append((a, b, "wrong door"))
        if trip and trip[-1]["to"] != b and not b.startswith("LIFT-"):
            bad.append((a, b, "does not end at the room"))
        if rm.floor_of(a) != rm.floor_of(b) and not (a.startswith("LIFT-") or b.startswith("LIFT-")) and len(trip) != 2:
            bad.append((a, b, "floors need two panels"))
check(f"all {len(places) * (len(places) - 1)} routes follow the corridor", not bad, bad[:5])

# 2. the door touches the drawn room, and each room is drawn on the floor hospital.json says
for u in plans.H["units"]:
    box = rm.BOXES.get(u["id"])
    if not box:
        check(f"{u['id']} is drawn", False)
        continue
    (dx, dy), _ = rm.door(u["id"]) if u.get("public") is not False else ((box[0], box[1]), 0)
    if not (box[0] <= dx <= box[0] + box[2] and dy in (box[1], box[1] + box[3])):
        check(f"{u['id']} door on its box", False, (dx, dy, box))
check("every room has a door on its own box", True)
check("staff-only rooms get no map", not rm.is_place("D-1-OT"))
check("made-up rooms get no map", not rm.is_place("Z-9-NOPE") and not rm.is_place("LIFT-9"))

# 3. the image really is a phone-sized JPEG, and the route is drawn on it
one = Image.open(io.BytesIO(rm.draw("GATE1", "B-G-SAMPLE")))
two = Image.open(io.BytesIO(rm.draw("CORE-G-PHARM", "B-2-ENDO")))
check("same-floor map is 1 panel", one.format == "JPEG" and one.width == rm.OUT_WIDTH, one.size)
check("two-floor map is 2 panels", two.width == rm.OUT_WIDTH and len(rm.legs("CORE-G-PHARM", "B-2-ENDO")) == 2, two.size)
check("map is small enough to send", len(rm.draw("CORE-G-PHARM", "B-2-ENDO")) < 600_000)
blue = sum(1 for p in one.convert("RGB").get_flattened_data() if p[2] > 170 and p[0] < 60)
check("route line is on the image", blue > 2000, blue)

# 4. the left/right words match the plan: walking east, rooms drawn above the corridor are on your left
check("Blood collection is left from Gate 1", rm.side("GATE1", "B-G-SAMPLE") == "left", rm.side("GATE1", "B-G-SAMPLE"))
check("Report counter is right from Gate 1", rm.side("GATE1", "B-G-REPORT") == "right", rm.side("GATE1", "B-G-REPORT"))
check("walking back west swaps the hands", rm.side("GATE2", "B-G-SAMPLE") == "right", rm.side("GATE2", "B-G-SAMPLE"))

# 5. where the patient is standing
for text, dest, want in [
    ("main pharmacy par hoon", None, "CORE-G-PHARM"),
    ("मैं फ़ार्मेसी पर हूँ", None, "CORE-G-PHARM"),
    ("I'm at the pharmacy", None, "CORE-G-PHARM"),
    ("gate 2 par khada hoon", None, "GATE2"),
    ("pharmacy se endoscopy kaise jaun", "B-2-ENDO", "CORE-G-PHARM"),
    ("from the pharmacy to endoscopy", "B-2-ENDO", "CORE-G-PHARM"),
    ("second floor par lift ke paas hoon", None, "LIFT-2"),
    ("chemo se pehle CBC blood test kahan hoga", "B-G-SAMPLE", None),
    ("where is the pharmacy", "CORE-G-PHARM", None),
    ("pharmacy kahan hai", "CORE-G-PHARM", None),
    ("radiology par jana hai", None, None),
    ("main radiology par jana chahta hoon", None, None),
    ("मैं रजिस्ट्रेशन डेस्क पे दिखाया, उन्होंने बोला ऑन्कोलॉजी सर्जरी डिपार्टमेंट में जाना है", "A-1-SURG", None),
    ("main registration mein hoon", None, "A-G-REG"),
]:
    got = brain.spoken_start(text, dest)
    check(f"start in {text!r}", (got[0] if got else None) == want, got)


def state():
    return {"pid": None, "mode": "chat", "awaiting": None, "where": None, "place": None, "hospital": "tata-parel"}


def ask(text, st):
    return asyncio.run(brain.respond(text, st))


# 6. the conversation: map after a direction, none after safety, redrawn when they say where they are
st = state()
out = ask("Where is the endoscopy room?", st)
check("direction carries a map from Gate 1", out.get("map", {}).get("from") == "GATE1" and out["map"]["to"] == "B-2-ENDO", out)
check("caption says the start is a guess", "standing somewhere else" in out.get("map", {}).get("caption", ""), out.get("map"))
out = ask("I'm at the pharmacy", st)
check("saying where you are redraws the route", out.get("map", {}).get("from") == "CORE-G-PHARM" and out["map"]["to"] == "B-2-ENDO", out)
check("words start from there, not Gate 1", "pharmacy" in out["text"].lower() and "gate 1" not in out["text"].lower(), out["text"])
out = ask("Where is the billing counter?", st)
check("next question starts from where they are", out.get("map", {}).get("from") == "CORE-G-PHARM", out)
for text in ("I have chest pain", "मुझे बहुत दर्द हो रहा है", "chemo ke baad tez bukhar hai"):
    out = ask(text, state())
    check(f"no map on a safety answer: {text}", "map" not in out, out)
st = state()
st["hospital"] = None
out = ask("Where is the endoscopy room?", st)
check("no map before we know the hospital", "map" not in out, out)

# 7. asking for a language by name
for text, want in [
    ("speak in tamil, i dont understand hindi", "ta"),
    ("speak in tamil", "ta"),
    ("Tamil", "ta"),
    ("kannada", "kn"),
    ("tamil mein bolo", "ta"),
    ("I only understand Kannada", "kn"),
    ("reply in english", "en"),
    ("हिंदी में बताइए", "hi"),
    ("தமிழில் பேசுங்கள்", None),      # an inflected "in Tamil": understood by the script instead
    ("தமிழ் மொழியில் பேசு", "ta"),
    ("Tamil Nadu se aaya hoon", None),
    ("I don't understand hindi", None),
    ("hindi nahi aati", None),
    ("where is endoscopy", None),
]:
    check(f"language in {text!r}", brain.asked_language(text) == want, brain.asked_language(text))

print("\nALL PASSED" if not fails else f"\n{fails} FAILED")
raise SystemExit(1 if fails else 0)
