"""Walkthrough map checks for the TMH booklet layout, no Sarvam calls except the conversation part. Run: python route_test.py"""
import asyncio
import sys
import io
import os
import tempfile

os.environ["ONCO_DB"] = os.path.join(tempfile.mkdtemp(), "route.db")
sys.stdout.reconfigure(encoding="utf-8")
from PIL import Image  # noqa: E402

import brain  # noqa: E402
import core  # noqa: E402
import route_map as rm  # noqa: E402

fails = 0


def check(name, ok, detail=""):
    global fails
    fails += not ok
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"  -> {detail}"))


# 1. every route between every pair of places: right panels, on the corridor or the campus path, ending at the right door
places = [p["id"] for p in rm.start_choices()]
bad = []
for a in places:
    for b in places:
        if a == b or b in rm.GATES:
            continue
        trip = rm.trip(a, b)
        if not trip or trip[-1]["kind"] != "floor" or trip[-1]["to"] != b:
            bad.append((a, b, "does not end at the room"))
            continue
        changes = a in rm.GATES or rm.building_of(a) != rm.building_of(b)
        if changes != any(leg["kind"] == "campus" for leg in trip):
            bad.append((a, b, "campus leg only when the building changes"))
        for leg in trip:
            if leg["kind"] == "campus":
                mid = rm.campus_points(leg["from"], leg["to"])[1:-1]
                if any(y != rm.SPINE_Y for _, y in mid):
                    bad.append((a, b, "left the campus path"))
                continue
            pts = rm.floor_points(leg["block"], leg["floor"], leg["from"], leg["to"])
            if {leg["from"], leg["to"]} != {f"LIFT-{leg['block']}-{leg['floor']}", "ENTRANCE"}:
                if any(y != rm.CORR_Y for _, y in pts[1:-1]):
                    bad.append((a, b, "left the corridor"))
                if pts[-1] != rm._door(leg["block"], leg["floor"], leg["to"])[0]:
                    bad.append((a, b, "wrong door"))
check(f"all {len(places) * (len(places) - 2)} routes follow the corridor and campus path", not bad, bad[:5])

# 2. every department is drawn on the building floor the booklet gives, with its door on its own box, no overlaps
problems = []
for b, f in rm.used_floors():
    boxes = rm.room_boxes(b, f)
    for sid, (x, y, w, h) in boxes.items():
        s = rm.SPOTS[sid]
        if (s["block"], s["floor"]) != (b, f):
            problems.append((sid, "on the wrong floor"))
        (dx, dy), _ = rm._door(b, f, sid)
        if not (x <= dx <= x + w and dy in (y, y + h)):
            problems.append((sid, "door off its box"))
        if not (60 < x and x + w < 1540 and 110 < y and y + h < 690) or (y < rm.CORR_Y1 and y + h > rm.CORR_Y0):
            problems.append((sid, "outside the floor or on the corridor"))
        for sid2, (x2, y2, w2, h2) in boxes.items():
            if sid < sid2 and x < x2 + w2 and x2 < x + w and y < y2 + h2 and y2 < y + h:
                problems.append((sid, sid2, "overlap"))
missing = [sid for sid, s in rm.SPOTS.items() if sid not in rm.room_boxes(s["block"], s["floor"])]
check(f"all {len(rm.SPOTS)} department places drawn on their own floor, no overlaps", not problems and not missing, (problems + missing)[:5])
check("staff-only rooms get no map", not rm.is_place("D-1-OT") and not rm.is_place("D-2-ICU"))
check("made-up places get no map", not any(rm.is_place(p) for p in ("Z-9-NOPE", "LIFT-9", "LIFT-GJB-9", "B-2-ENDO", "C-1-MRI@3")))

# 3. the image really is a phone-sized JPEG, and the route is drawn on it
one = Image.open(io.BytesIO(rm.draw("GATE1", "A-G-REG")))
three = rm.draw("CORE-G-PHARM@private", "C-1-MRI")
check("gate to a ground-floor desk is 2 panels", one.format == "JPEG" and one.width == rm.OUT_WIDTH and len(rm.trip("GATE1", "A-G-REG")) == 2, one.size)
check("building to building from inside is 3 panels", len(rm.trip("CORE-G-PHARM@private", "C-1-MRI")) == 3)
check("map is small enough to send", len(three) < 600_000, len(three))
blue = sum(1 for p in one.convert("RGB").get_flattened_data() if p[2] > 170 and p[0] < 60)
check("route line is on the image", blue > 2000, blue)

# 4. the places are where the TMH booklet says
U = brain.PLACES
facts = [
    ("registration: GJB ground (general), HBB 1st (private)", (U["A-G-REG"]["block"], U["A-G-REG"]["floor"], U["A-G-REG"]["private"]["block"],
                                                               U["A-G-REG"]["private"]["floor"]) == ("GJB", "G", "HBB", "1")),
    ("scan tokens: HBB 1st floor Rotunda, counters 129 to 131", (U["C-1-RADREC"]["block"], U["C-1-RADREC"]["floor"]) == ("HBB", "1")
     and "129" in U["C-1-RADREC"]["rooms"]),
    ("casualty: MB ground floor, 24 hours", (U["D-G-ER"]["block"], U["D-G-ER"]["floor"]) == ("MB", "G") and "24" in U["D-G-ER"]["hours"]),
    ("MRI: GJB basement (general), MB basement (private)", (U["C-1-MRI"]["block"], U["C-1-MRI"]["private"]["block"]) == ("GJB", "MB")),
    ("endoscopy is not in the booklet: sent to the Helpdesk", U["B-2-ENDO"].get("unlisted") and U["B-2-ENDO"]["ask"] == "A-G-HELP"),
    ("PET: take the token first", U["C-2-PET"].get("unlisted") and U["C-2-PET"]["ask"] == "C-1-RADREC"),
]
for name, ok in facts:
    check(name, bool(ok))

# 5. where the patient is standing
for text, dest, want in [
    ("main pharmacy par hoon", None, "CORE-G-PHARM"),
    ("मैं फ़ार्मेसी पर हूँ", None, "CORE-G-PHARM"),
    ("I'm at the pharmacy", None, "CORE-G-PHARM"),
    ("back gate par khada hoon", None, "GATE2"),
    ("main gate par hoon", None, "GATE1"),
    ("pharmacy se mri kaise jaun", "C-1-MRI", "CORE-G-PHARM"),
    ("from the pharmacy to the MRI", "C-1-MRI", "CORE-G-PHARM"),
    ("second floor par lift ke paas hoon", "A-3-HN@private", "LIFT-HBB-2"),
    ("main GJB mein lift ke paas hoon", None, "LIFT-GJB-G"),
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


# 6. the conversation: map after a direction, the private location once they say so, none after safety
st = state()
out = ask("Where is the MRI?", st)
check("direction carries a map from the main gate", out.get("map", {}).get("from") == "GATE1" and out["map"]["to"] == "C-1-MRI", out)
check("answer gives both booklet places", "Golden Jubilee" in out["text"] and "Main Building" in out["text"], out["text"])
check("caption says the start is a guess", "standing somewhere else" in out.get("map", {}).get("caption", ""), out.get("map"))
out = ask("I'm a private patient", st)
check("'private patient' re-answers for private patients", out.get("map", {}).get("to") == "C-1-MRI@private" and "Main Building" in out["text"], out)
out = ask("I'm at the pharmacy", st)
check("saying where you are redraws the route", out.get("map", {}).get("from") == "CORE-G-PHARM@private"
      and out["map"]["to"] == "C-1-MRI@private", out)
check("words start from there, not the main gate", "dispensary" in out["text"].lower() and "main gate" not in out["text"].lower(), out["text"])
out = ask("Where is the cash deposit counter?", st)
check("next question starts from where they are", out.get("map", {}).get("from") == "CORE-G-PHARM@private", out)
out = ask("Where is endoscopy?", state())
check("a place not in the booklet says so and maps to the Helpdesk", "booklet" in out["text"] and out.get("map", {}).get("to") == "A-G-HELP", out)
for text in ("I have chest pain", "मुझे बहुत दर्द हो रहा है", "chemo ke baad tez bukhar hai"):
    out = ask(text, state())
    check(f"no map on a safety answer: {text}", "map" not in out, out)
st = state()
st["hospital"] = None
out = ask("Where is the MRI?", st)
check("no map before we know the hospital", "map" not in out, out)
check("help desk wording is the booklet's", "Rotunda" in core.HELP_DESK["en"] and "गेट 1" not in brain.UNKNOWN["hi"])

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
