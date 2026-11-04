"""The OncoSaathi "brain": emergency check, clinical guardrail, then room lookup.

Every reply is a fixed, pre-approved sentence. The optional Sarvam LLM step can only
pick one of the known labels; it never writes the answer text.
"""
import json
import re
from pathlib import Path

HOSPITAL = json.loads(Path(__file__).with_name("hospital_tmh.json").read_text(encoding="utf-8"))
BUILDINGS = HOSPITAL["buildings"]

FLOORS = {
    "en": {"B": "Basement", "G": "Ground Floor", "1": "First Floor", "2": "Second Floor", "3": "Third Floor", "4": "Fourth Floor",
           "5": "Fifth Floor", "6": "Sixth Floor", "7": "Seventh Floor", "8": "Eighth Floor"},
    "mr": {"B": "तळघर", "G": "तळमजला", "1": "पहिला मजला", "2": "दुसरा मजला", "3": "तिसरा मजला", "4": "चौथा मजला",
           "5": "पाचवा मजला", "6": "सहावा मजला", "7": "सातवा मजला", "8": "आठवा मजला"},
}
FLOORS["hi"] = FLOORS["en"]
WORDS = {
    "en": {"general": "General patients", "private": "Private patients", "or": "or", "room": "Room"},
    "hi": {"general": "General patients ke liye", "private": "Private patients ke liye", "or": "ya", "room": "Room"},
    "mr": {"general": "जनरल रुग्णांसाठी", "private": "खासगी रुग्णांसाठी", "or": "किंवा", "room": "खोली"},
}


def describe(loc: dict, lang: str) -> str:
    text = f"{BUILDINGS[loc['building']][lang]}, {FLOORS[lang][loc['floor']]}"
    room = loc.get("room")
    if room:
        text += f", {WORDS[lang]['room']} {room}" if room[0].isdigit() else f", {room}"
    return text


def build_reply(place: dict, lang: str) -> str:
    """Compose the sentence from the data, so the spoken answer can never disagree with the map."""
    words, parts = WORDS[lang], [place["name"][lang] + "."]
    for category in ("all", "general", "private"):
        spots = [describe(loc, lang) for loc in place["locations"] if loc["category"] == category]
        if spots:
            joined = f" {words['or']} ".join(spots)
            parts.append(f"{joined}." if category == "all" else f"{words[category]}: {joined}.")
    if "note" in place:
        parts.append(place["note"][lang])
    return " ".join(parts)


ROOMS = {
    intent: {**place, "reply": {f"{lang}-IN": build_reply(place, lang) for lang in ("hi", "mr", "en")}}
    for intent, place in HOSPITAL["places"].items()
}

# Checked before anything else. The reply sends the patient to Emergency, nothing more.
EMERGENCY = [
    "casualty", "emergency", "इमरजेंसी", "आपत्कालीन", "कैजुअल्टी",
    "saans nahi", "saans nahin", "sans nahi", "saans lene mein", "सांस नहीं", "साँस नहीं", "श्वास घेता येत नाही",
    "khoon aa raha", "khoon beh raha", "bleeding", "खून आ रहा", "खून बह रहा",
    "behosh", "बेहोश", "unconscious", "chest pain", "seene mein dard", "सीने में दर्द", "can't breathe", "cannot breathe",
]

# Words that make a question medical. Checked before room lookup: safety wins every tie.
CLINICAL = [
    "pain", "painful", "hurt", "hurts", "hurting", "ache", "aching", "nausea", "rash", "itching",
    "swelling", "dizzy", "symptom", "symptoms", "dard", "दर्द", "वेदना", "fever", "bukhar", "बुखार", "ताप",
    "medicine", "dawai", "davai", "dawa", "dava", "दवा", "दवाई", "औषध", "tablet", "goli", "गोली", "dose",
    "side effect", "vomiting", "ulti", "उल्टी", "उलटी", "khujli", "sujan", "सूजन",
    "report mein kya", "report me kya", "report normal", "रिपोर्ट में क्या",
    "kya khana", "kitni baar", "treatment", "ilaaj", "इलाज", "cancer stage", "stage kya",
]

# Logistics phrases that happen to contain a clinical word.
SAFE_PHRASES = [
    "dawai ki dukaan", "davai ki dukaan", "dawai counter", "davai counter",
    "दवाई की दुकान", "दवा की दुकान", "औषध दुकान", "medical store",
    "pain clinic", "पेन क्लिनिक",
]

EMERGENCY_REPLY = {
    "hi-IN": "Yeh emergency ho sakti hai. Abhi turant Main Building, Ground Floor par Casualty jaiye, ya paas ke staff ko bulaiye.",
    "mr-IN": "ही आपत्कालीन स्थिती असू शकते. आत्ताच मुख्य इमारत, तळमजला येथील कॅज्युअल्टी विभागात जा, किंवा जवळच्या कर्मचाऱ्यांना बोलवा.",
    "en-IN": "This may be an emergency. Please go to Casualty in the Main Building, Ground Floor right now, or call the nearest staff member.",
}
# Mentor requirement: every answer tells the patient this is an AI tool.
DISCLAIMER = {
    "hi-IN": "Yeh AI se chalne wali seva hai aur doctor ki salah nahi hai. Galti ho sakti hai, kripya help desk se confirm kar lein.",
    "mr-IN": "ही एआयवर चालणारी सेवा आहे आणि डॉक्टरांचा सल्ला नाही. चूक होऊ शकते, कृपया मदत कक्षात खात्री करून घ्या.",
    "en-IN": "This is an AI service, not medical advice. It can make mistakes, so please confirm at the help desk.",
}
DOCTOR_REPLY = {
    "hi-IN": "Yeh sawaal aapke doctor ke liye hai. Kripya apne treating doctor se baat karein.",
    "mr-IN": "हा प्रश्न तुमच्या डॉक्टरांसाठी आहे. कृपया तुमच्या उपचार करणाऱ्या डॉक्टरांशी बोला.",
    "en-IN": "This question is for your doctor. Please speak with your treating doctor.",
}
DONT_KNOW = {
    "hi-IN": "Maaf kijiye, samajh nahi aaya. Kya aap dobara bol sakte hain? Ya help desk par poochiye.",
    "mr-IN": "माफ करा, समजले नाही. कृपया पुन्हा सांगाल का? किंवा मदत कक्षात विचारा.",
    "en-IN": "Sorry, I didn't catch that. Please say it again, or ask at the help desk.",
}

LLM_LABELS = list(ROOMS) + ["clinical", "emergency", "unknown"]


def clean(text: str) -> str:
    """Lower-case, drop punctuation, pad with spaces so we can match whole words."""
    text = re.sub(r"[?!.,;:\"'()।॥-]", " ", text.lower())
    return " " + " ".join(text.split()) + " "


def has(phrase: str, t: str) -> bool:
    return f" {clean(phrase).strip()} " in t


def pick(replies: dict, lang: str) -> tuple[str, str]:
    """Return (reply text, language of that text). Falls back to English, the source for translation."""
    for code in (lang, "en-IN", "hi-IN"):
        if code in replies:
            return replies[code], code
    raise KeyError("reply missing hi-IN and en-IN")


def keyword_intent(text: str) -> str:
    t = clean(text)
    if not t.strip():
        return "unclear"
    if any(has(w, t) for w in EMERGENCY):
        return "emergency"
    t_check = t
    for safe in SAFE_PHRASES:
        t_check = t_check.replace(f" {clean(safe).strip()} ", " ")
    if any(has(w, t_check) for w in CLINICAL):
        return "clinical"
    # Most keyword hits wins; ties go to the longest matched keyword
    # ("chemo se pehle CBC blood test" -> blood_test, not chemo_daycare).
    best, best_score = "unknown", (0, 0)
    for intent, info in ROOMS.items():
        hits = [k for k in info["keywords"] if has(k, t)]
        score = (len(hits), max((len(k) for k in hits), default=0))
        if score > best_score:
            best, best_score = intent, score
    return best


def reply_for(intent: str, lang: str) -> tuple[str, str]:
    if intent == "emergency":
        return pick(EMERGENCY_REPLY, lang)
    if intent == "clinical":
        return pick(DOCTOR_REPLY, lang)
    if intent in ROOMS:
        return pick(ROOMS[intent]["reply"], lang)
    return pick(DONT_KNOW, lang)


def locations_for(intent: str) -> list[dict]:
    """Where to highlight on the map. Safety and unclear replies have no destination, except Casualty."""
    if intent == "emergency":
        return HOSPITAL["emergency_locations"]
    return ROOMS[intent]["locations"] if intent in ROOMS else []


def route(text: str, lang: str = "hi-IN", classify=None) -> tuple[str, str, str]:
    """Return (answer, intent, answer_language).

    `classify` is an optional function(text) -> label used only when keywords find
    nothing; its label must be one of LLM_LABELS or it is ignored.
    """
    intent = keyword_intent(text)
    if intent == "unknown" and classify is not None:
        try:
            label = classify(text)
        except Exception:
            label = None
        if label in LLM_LABELS:
            intent = label
    answer, answer_lang = reply_for(intent, lang)
    return answer, intent, answer_lang
