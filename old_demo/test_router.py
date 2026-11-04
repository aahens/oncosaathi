"""Offline checks for the router (no Sarvam calls). Run: python test_router.py"""
from router import BUILDINGS, DISCLAIMER, HOSPITAL, ROOMS, keyword_intent, locations_for

CASES = [
    # logistics
    ("Doctor ne chemo se pehle CBC blood test bola hai, kahan jayein?", "blood_test"),
    ("डॉक्टर ने chemo से पहले CBC blood test बोला है कहाँ जाएँ?", "blood_test"),
    ("chemo room kahan hai?", "chemo_daycare"),
    ("dawai ki dukaan kahan hai?", "pharmacy"),
    ("दवाई की दुकान कहाँ है", "pharmacy"),
    ("new patient registration kahan hota hai", "registration"),
    ("PET-CT scan kidhar hota hai?", "scan"),
    ("report kab milega", "report_collection"),
    ("रक्त तपासणी कुठे आहे?", "blood_test"),
    ("Where is the pharmacy?", "pharmacy"),
    # clinical
    ("mujhe bahut dard ho raha hai", "clinical"),
    ("मुझे बुखार है, कौन सी दवाई लूँ?", "clinical"),
    ("What dose of medicine should I take?", "clinical"),
    ("chemo ke baad dard ho raha hai, chemo room kahan hai?", "clinical"),
    ("report mein kya aaya?", "clinical"),
    ("ulti ho rahi hai chemo ke baad", "clinical"),
    # emergency
    ("saans nahi aa rahi", "emergency"),
    ("khoon aa raha hai", "emergency"),
    ("casualty kahan hai", "emergency"),
    # Tata Memorial places
    ("canteen kahan hai", "cafeteria"),
    ("pathology department kahan hai", "pathology"),
    ("CT scan kahan hoga", "ct_scan"),
    ("MRI scan kidhar hai", "mri"),
    ("X-ray kahan hota hai", "radiology"),
    ("mammography kahan hai", "mammography"),
    ("smart card top up kahan hoga", "cash_counter"),
    ("Ayushman card ke liye kahan jayein", "social_worker"),
    ("pain clinic kahan hai", "pain_clinic"),
    ("blood donation kahan hota hai", "blood_donation"),
    ("kevat kahan milega", "kevat"),
    # a place name must not hide a medical question
    ("bahut dard hai, pain clinic kahan hai", "clinical"),
    # nothing matched
    ("toilet kahan hai", "unknown"),
    ("", "unclear"),
]

failed = 0
for text, expected in CASES:
    got = keyword_intent(text)
    mark = "ok  " if got == expected else "FAIL"
    failed += got != expected
    print(f"{mark} {expected:18} {got:18} {text}")
print(f"\n{len(CASES) - failed}/{len(CASES)} passed")

# The map and the spoken answers come from one file, so check that file is complete.
problems = []
for intent, place in ROOMS.items():
    for code in ("hi-IN", "mr-IN", "en-IN"):
        if not place["reply"].get(code):
            problems.append(f"{intent}: no {code} reply")
    for key in ("name", "note"):
        if key in place and set(place[key]) != {"hi", "mr", "en"}:
            problems.append(f"{intent}: {key} missing a language")
    for loc in place["locations"]:
        if loc["floor"] not in BUILDINGS[loc["building"]]["floors"]:
            problems.append(f"{intent}: {loc['building']} floor {loc['floor']} is not on the map")
        if loc["category"] not in ("all", "general", "private"):
            problems.append(f"{intent}: bad category {loc['category']}")
    if not place.get("source"):
        problems.append(f"{intent}: no booklet source")
if locations_for("clinical") or locations_for("unknown"):
    problems.append("clinical and unknown replies must not point to a place")
if not locations_for("emergency") or len(DISCLAIMER) != 3 or not HOSPITAL["hospital"]["source_url"]:
    problems.append("emergency location, disclaimer or source link missing")
print(*problems or [f"data ok: {len(ROOMS)} places"], sep="\n")
raise SystemExit(1 if failed or problems else 0)
