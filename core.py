"""OncoSaathi core: hospital directory (hospital.json), safety keyword lists, language pick, Sarvam STT/TTS helpers.

The conversation logic lives in brain.py; the patient memory in patient_store.py. Nothing here invents a room:
every place comes from hospital.json.
"""
import base64
import json
import mimetypes
import os
import re
import tempfile

import requests
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))

SARVAM_KEY = os.environ.get("SARVAM_API_KEY", "")  # read from .env, never hardcoded
# bulbul:v3 "ritu": ~1.5 s per sentence. bulbul:v4-flash (ritu_hi_medical) measured 6-14 s per sentence on 20 Oct 2026,
# too slow for a live call. Switch back with ONCO_TTS_MODEL=bulbul:v4-flash in .env.
TTS_MODEL = os.environ.get("ONCO_TTS_MODEL", "bulbul:v3").strip()
TTS_VOICE = ({"hi": "ritu_hi_medical", "en": "ritu_en_medical"} if TTS_MODEL.startswith("bulbul:v4")
             else {"hi": "ritu", "en": "ritu"})
STT_MODEL = "saaras:v4"                                          # latest Sarvam STT
KEYTERMS = ["CBC", "OPD", "pathology", "biopsy", "chemo", "chemotherapy", "radiation", "PET CT", "CT scan", "MRI", "UHID",
            "Ayushman", "Tata Memorial", "endoscopy", "dharamshala", "GJB", "HBB", "Homi Bhabha", "Golden Jubilee", "Rotunda", "Kevat",
            "daycare", "smart card"]
SARVAM = "https://api.sarvam.ai"

# ---------------- Safety keywords (checked before anything else, deterministic) ----------------
SELF_HARM = ["suicide", "kill myself", "end my life", "want to die", "jeena nahi", "jina nahi", "mar jana", "mar jaun", "mar jaaun",
             "khud ko khatam", "zindagi khatam", "आत्महत्या", "जीना नहीं", "मर जाना", "मर जाऊँ", "मर जाऊं", "ख़ुद को ख़त्म", "खुद को खत्म",
             "ज़िंदगी ख़त्म", "जिंदगी खत्म"]
EMERGENCY = ["सांस नहीं", "साँस नहीं", "सांस फूल", "साँस फूल", "बेहोश", "खून बह", "सीने में दर्द", "chest pain", "can't breathe",
             "cannot breathe", "not breathing", "unconscious", "bleeding", "emergency", "इमरजेंसी", "तेज़ बुखार",
             "तेज बुखार", "दौरा", "seizure", "behosh", "saans nahi", "tez bukhar", "tej bukhar", "high fever", "saans lene",
             "sans lene", "सांस लेने", "साँस लेने", "breathing problem", "difficulty breathing", "khoon beh", "saans phool",
             "seene mein dard", "seene me dard", "dora pad", "faint", "एमरजेंसी", "इमर्जेंसी", "emergency"]
CLINICAL = ["कितनी दवा", "कौन सी दवा", "कौनसी दवा", "दवा लूँ", "दवा लू", "which medicine", "what medicine", "dose", "dosage",
            "डोज़", "डोज", "गोली", "दर्द", "pain", "बुखार", "fever", "उल्टी", "vomit", "side effect", "साइड इफेक्ट",
            "साइड इफ़ेक्ट", "ठीक हो जा", "will i survive", "बचूँगा", "बचेंगे", "stage", "स्टेज", "रिपोर्ट का मतलब",
            "रिपोर्ट में क्या", "what does my report", "कैंसर फैल", "spread", "क्या मुझे कैंसर", "do i have cancer",
            "हीमोग्लोबिन", "haemoglobin", "hemoglobin", "शुगर बढ़", "क्या खाऊँ", "what should i eat", "dawa lu", "dawa le",
            "kaun si dawa", "kaunsi dawa", "dawa kab", "dawa kitni", "tablet kab", "dard", "bukhar", "report saying",
            "report says", "report mean", "is my report", "report me kya", "report mein kya", "report normal", "report theek",
            "report thik", "theek ho jaung", "thik ho jaung", "theek ho jaaung", "बच जाऊँ", "bach jaung", "रिपोर्ट ठीक",
            "रिपोर्ट नॉर्मल", " ulti ", "chakkar"]
EMOTIONAL = ["डर लग", "डर रहा", "डरी", "डर गई", "डर गया", "dar lag", "dar gayi", "dar gaya", "ghabra", "akela", "akeli", "ro rahi",
             "ro raha", "घबराहट", "घबरा", "अकेला", "अकेली", "रोना", "टूट गया", "टूट गई", "scared", "afraid", "frightened",
             "depressed", "anxious", "परेशान हूँ", "himmat nahi", "हिम्मत नहीं", "tension"]

HINGLISH = ["kahan", "kaha", "kya", "hai", "mera", "meri", "mujhe", "kab", "kaise", "karna", "jana", "jaana", "milegi", "dawa",
            "dard", "nahi", "aage", "chahiye", "kitna", "paisa", "khoon", "likha", "naam", "hoon", "aaj", "kal", "ho", "gaya",
            "ki", "ka", "ke", "mein", "se", "hain", "batao", "bataiye", "jagah", "liye", "aur", "bhi", "toh", "karo", "wala",
            "wali", "koi", "kuch", "abhi", "yahan", "wahan", "rehne", "rahne", "sasta", "sasti", "saste", "lana", "laana",
            "kidhar", "jaun", "jaana", "hum", "aap", "mujhe", "accha", "achha", "theek", "thik", "haan", "ji"]


# Sarvam language codes. Hindi and English answers are written by our templates; the others are translated
# (sarvam-translate:v1) from the English answer, so the facts stay the same in every language.
LANG_CODE = {"hi": "hi-IN", "en": "en-IN", "mr": "mr-IN", "od": "od-IN", "bn": "bn-IN", "gu": "gu-IN", "pa": "pa-IN",
             "ta": "ta-IN", "te": "te-IN", "kn": "kn-IN", "ml": "ml-IN"}
SCRIPTS = [("od", "\u0B00", "\u0B7F"), ("bn", "\u0980", "\u09FF"), ("gu", "\u0A80", "\u0AFF"), ("pa", "\u0A00", "\u0A7F"),
           ("ta", "\u0B80", "\u0BFF"), ("te", "\u0C00", "\u0C7F"), ("kn", "\u0C80", "\u0CFF"), ("ml", "\u0D00", "\u0D7F")]
MARATHI = re.compile(r"(?<![\u0900-\u097F])(आहे|आहेत|काय|कुठे|मला|मी|तुम्ही|कसे|कधी|नाही|पाहिजे|जायचे|सांगा)(?![\u0900-\u097F])")


def pick_lang(text, setting="Auto", stt_lang=None):
    if setting == "English":
        return "en"
    if setting == "हिंदी":
        return "hi"
    for code, lo, hi_ in SCRIPTS:                    # Odia, Bengali, Tamil… by script
        if re.search(f"[{lo}-{hi_}]", text or ""):
            return code
    if re.search(r"[ऀ-ॿ]", text or ""):          # Devanagari: Marathi if it reads like Marathi, else Hindi
        if (stt_lang or "").startswith("mr") or len(MARATHI.findall(text)) >= 1:
            return "mr"
        return "hi"
    words = re.findall(r"[a-z]+", (text or "").lower())
    if sum(w in HINGLISH for w in words) >= 1:              # romanised Hinglish -> Hindi
        return "hi"
    if stt_lang and stt_lang.startswith("hi"):
        return "hi"
    return "en"


def hit(k, t):
    # short keys must match a whole word: "doctor" mustn't trigger "ct", and "साइकोलॉजिकल" mustn't trigger "इको" (echo)
    if k.isascii() and len(k) <= 4:
        return re.search(r"\b" + re.escape(k) + r"\b", t) is not None
    if not k.isascii() and len(k) <= 5:
        return re.search(r"(?<![\u0900-\u097F])" + re.escape(k) + r"(?![\u0900-\u097F])", t) is not None
    return k in t


# ---------------- Hospital directory (hospital.json) ----------------
with open(os.path.join(HERE, "hospital.json"), encoding="utf-8") as _f:
    HOSPITAL = json.load(_f)
BLOCKS = {b["id"]: b for b in HOSPITAL["blocks"]}
FLOORS = {f["id"]: f for f in HOSPITAL["floors"]}
NAME_STOP = {"scan", "test", "tests", "clinic", "room", "rooms", "ward", "wards", "desk", "counter", "department", "unit",
             "area", "lounge", "hall", "centre", "center", "main", "oncology", "care", "and", "with", "office", "lab", "labs",
             "treatment", "before", "after", "only", "exit", "क्लिनिक", "वार्ड", "कक्ष", "और", "लैब", "डेस्क", "केंद्र", "विभाग", "ऑफ़िस",
             "इलाज", "रास्ता", "जाने", "आने", "बाहर", "पहले", "बाद", "लिए", "वाले", "सभी", "समय", "जाँच", "जांच", "सिर्फ़", "करना",
             "गेट", "मुख्य", "बैठने", "जगह", "परिवार", "स्कैन", "काउंटर", "कमरा", "hospital", "अस्पताल", "हॉस्पिटल",
             # words in many booklet department names, or about the patient: never enough on their own to pick a place
             "opd", "ओपीडी", "patient", "patients", "मरीज़", "मरीज", "पेशेंट", "general", "जनरल", "private", "प्राइवेट", "medical",
             "मेडिकल", "new", "नए", "old", "पुराने", "building", "बिल्डिंग", "first", "token", "टोकन", "help", "मदद",
             # everyday words that the booklet's Hindi names brought in ("योजनाओं में मदद", "बच्चों का कैंसर ओपीडी")
             "में", "कैंसर", "cancer", "ऑपरेशन", "operation", "medicine", "मेडिसिन", "सेंटर", "रोग"}
# Colloquial / Hinglish ways people ask for each department (matched in addition to the department's own name words).
# Ids are the hospital.json unit ids (TMH booklet layout, see build_hospital.py).
SYNONYMS = {
    "A-G-HELP": ["help desk", "helpdesk", "enquiry", "inquiry", "पूछताछ", "हेल्पडेस्क", "हेल्प डेस्क", "lost", "khoya", "rotunda", "रोटुंडा"],
    "HELPLINE": ["wheelchair", "व्हीलचेयर", "stretcher", "स्ट्रेचर", "helpline", "हेल्पलाइन"],
    "KEVAT": ["kevat", "केवट", "navigator", "नेविगेटर", "guide", "गाइड"],
    "A-G-REG": ["registration", "register", "parcha", "parchi banani", "naya parcha", "new patient", "uhid card", "case file", "रजिस्ट्रेशन",
                "पर्चा", "नया मरीज़", "नया पर्चा", "केस फ़ाइल"],
    "SMARTCARD": ["smart card", "smartcard", "स्मार्ट कार्ड", "naya card", "card kho"],
    "A-G-BILL": ["billing", "bill", "payment", "pay", "paise jama", "fees", "cash counter", "top up", "top-up", "recharge", "बिल", "पैसे जमा",
                 "फ़ीस", "कैश काउंटर", "रिचार्ज"],
    "ACCOUNTS": ["refund", "रिफ़ंड", "रिफंड", "accounts"],
    "A-G-SW": ["social worker", "सोशल वर्कर", "msw", "financial", "paisa nahi", "paise nahi", "aarthik", "free treatment", "ngo", "scheme",
               "yojana", "आर्थिक", "योजना", "पैसे नहीं"],
    "A-G-PMJAY": ["ayushman", "pmjay", "pm-jay", "आयुष्मान"],
    "INSURANCE": ["insurance", "mediclaim", "reimbursement", "इंश्योरेंस", "मेडिक्लेम", "cro"],
    "PRO": ["complaint", "shikayat", "शिकायत", "feedback", "grievance"],
    "D-4-ADMIN": ["superintendent", "medical superintendent", "सुपरिंटेंडेंट"],
    "ADMIN-OFF": ["admin", "administration", "प्रशासन"],
    "ADMIT": ["admission", "admit", "bharti", "bharti hona", "भर्ती", "एडमिशन"],
    "MEDREC": ["medical records", "records department", "मेडिकल रिकॉर्ड"],
    "TELECONSULT": ["teleconsultation", "online consultation", "video consultation", "टेलीकंसल्टेशन"],
    "A-G-CANT": ["canteen", "cafeteria", "khana", "food", "कैंटीन", "खाना", "paani", "water", "chai", "चाय"],
    "ATM": ["atm", "एटीएम", "cash nikal", "पैसे निकाल"],
    "BANK": ["bank", "बैंक"],
    "STORAGE": ["luggage", "samaan", "saman", "सामान", "cloak room", "bag rakhna"],
    "CORE-G-PHARM": ["pharmacy", "pharmacology", "pharmacist", "dispensary", "डिस्पेंसरी", "फार्माकोलॉजी", "medical store", "chemist",
                     "medicine kahan", "medicines kahan", "medicine counter", "medicine lena",
                     "dawai kahan", "dawa kahan", "दवाई कहाँ", "दवा कहाँ", "फ़ार्मेसी", "फार्मेसी"],
    "B-G-SAMPLE": ["blood collection", "ब्लड कलेक्शन", "blood test", "blood sample", "sample collection", "sample dena", "khoon ki jaanch",
                   "khoon ki janch", "khoon", "cbc", "urine", "peshab", "खून", "पेशाब", "ब्लड", "सैंपल"],
    "B-G-REPORT": ["report counter", "report kahan", "report kab", "report collect", "report enquiry", "रिपोर्ट कहाँ", "रिपोर्ट कब",
                   "रिपोर्ट कलेक्ट", "रिपोर्ट लेनी"],
    "C-1-RADREC": ["scan booking", "scan token", "scan department", "token counter", "imaging", "इमेजिंग", "स्कैन टोकन"],
    "C-G-XRAY": ["x-ray", "xray", "x ray", "एक्स-रे", "एक्सरे", "radiology", "radiography", "radiographer", "radiodiagnosis",
                 "radio diagnosis", "रेडियोलॉजी", "रेडिओलॉजी", "रेडियोलॉजी डिपार्टमेंट"],
    "C-1-CT": ["ct scan", "ct", "सीटी", "सिटी स्कैन"],
    "C-1-MRI": ["mri", "एमआरआई"],
    "C-2-PET": ["pet scan", "pet-ct", "pet ct", "pet", "spect", "पेट-सीटी", "पेट सीटी", "पीईटी"],
    "C-1-USG": ["mammography", "mammogram", "मैमोग्राफी", "मैमोग्राफ़ी"],
    "USG": ["usg", "ultrasound", "sonography", "sonografi", "सोनोग्राफी", "अल्ट्रासाउंड"],
    "IR-OPD": ["interventional radiology", "interventional", "इंटरवेंशनल"],
    "B-1-PATH": ["pathology", "पैथोलॉजी", "biopsy", "slide", "slides", "block", "histopathology", "बायोप्सी", "स्लाइड"],
    "CYTO": ["cytology", "cytopathology", "fnac", "साइटोलॉजी"],
    "BIOCHEM": ["biochemistry", "बायोकेमिस्ट्री"],
    "MICRO": ["microbiology", "culture", "माइक्रोबायोलॉजी"],
    "MOLPATH": ["molecular", "haematopathology", "hematopathology", "मॉलिक्यूलर"],
    "B-3-PAC": ["pac", "fitness", "anaesthesia", "anesthesia", "operation se pehle", "ऑपरेशन से पहले"],
    "B-3-CARDIO": ["ecg", "heart test", "ईसीजी"],
    "ECHO": ["echo", "2d echo", "इको"],
    "PFT": ["pft", "lung test", "lung function", "फेफड़ों की जाँच"],
    "A-1-MED": ["medical oncology", "chemo doctor", "chemo wale doctor", "medicine department", "मेडिकल ऑन्कोलॉजी"],
    "A-1-SURG": ["surgeon", "surgery opd", "surgery wale doctor", "operation wale doctor", "oncology surgery", "onco surgery", "oncosurgery",
                 "surgery department", "surgical department", "ऑन्कोलॉजी सर्जरी", "सर्जरी डिपार्टमेंट", "सर्जरी विभाग", "सर्जिकल"],
    "A-1-RTC": ["radiation doctor", "radiation consult", "radiation opd"],
    "A-3-BR": ["breast", "स्तन", "ब्रेस्ट"],
    "A-3-HN": ["head and neck", "head & neck", "mouth cancer", "muh ka", "gale ka", "throat", "मुँह", "गले"],
    "GASTRO-OPD": ["gastro", "gi opd", "liver", "intestine", "colon", "rectum", "food pipe", "oesophagus", "esophagus", "आंत", "आँत", "लिवर"],
    "A-3-GYN": ["gynae", "gyne", "gynec", "cervix", "uterus", "ovary", "bachchedani", "बच्चेदानी", "स्त्री रोग"],
    "URO-OPD": ["uro", "prostate", "kidney", "bladder", "प्रोस्टेट", "किडनी"],
    "THOR-OPD": ["thoracic", "lung cancer", "chest cancer", "फेफड़े का कैंसर"],
    "BONE-OPD": ["bone", "sarcoma", "soft tissue", "हड्डी"],
    "NEURO-OPD": ["neuro", "brain", "brain tumour", "brain tumor", "दिमाग"],
    "HEME-OPD": ["blood cancer", "leukemia", "leukaemia", "lymphoma", "myeloma", "haematology", "hematology", "ब्लड कैंसर"],
    "A-2-PEDS": ["children", "child", "bachche", "bachcha", "bachchon", "बच्चे", "बच्चा", "बच्चों", "paediatric", "pediatric"],
    "DENTAL": ["dental", "teeth", "tooth", "daant", "dant", "दांत", "दाँत"],
    "A-2-DC": ["chemo", "chemotherapy", "day care", "daycare", "kimo", "कीमो", "डे केयर", "डेकेयर", "drip"],
    "D-G-RT": ["radiation", "radiotherapy", "रेडियोथेरेपी", "रेडियोथेरपी", "sikai", "sekai", "सिकाई", "सेकाई", "रेडिएशन", "rt card", "linac"],
    "RMC": ["radiation medicine", "nuclear medicine", "nuclear", "रेडिएशन मेडिसिन"],
    "A-4-PALL": ["palliative", "पैलिएटिव"],
    "PAIN": ["pain clinic", "पेन क्लिनिक", "दर्द क्लिनिक"],
    "A-4-PSY": ["counselling", "counseling", "counsellor", "psycho", "psychological", "psychology", "psychologist", "psychiatrist", "psychiatry",
                "काउंसलर", "काउंसलिंग", "साइकोलॉजिकल", "साइकोलॉजी", "मनोवैज्ञानिक", "mann ki baat"],
    "A-4-NUT": ["diet", "dietician", "nutrition", "khana kya", "आहार", "डाइट"],
    "PHYSIO": ["physiotherapy", "physio", "फ़िज़ियोथेरेपी", "फिजियोथेरेपी"],
    "OCC-THER": ["occupational therapy"],
    "SPEECH": ["speech therapy", "speech", "bolne", "स्पीच"],
    "STOMA": ["stoma", "colostomy", "स्टोमा"],
    "PREVENT": ["screening", "preventive", "cancer check", "स्क्रीनिंग"],
    "GENMED": ["physician", "general medicine", "फ़िज़िशियन"],
    "PULM": ["pulmonary", "पल्मोनरी"],
    "BLOODBANK": ["blood bank", "blood donation", "blood donate", "donor", "रक्तदान", "ब्लड बैंक", "ब्लड सेंटर"],
    "D-G-ER": ["emergency", "casualty", "इमरजेंसी", "कैज़ुअल्टी"],
    "D-2-ICU": ["icu", "hdu", "आईसीयू"],
    "D-1-OT": ["operation theatre", "operation theater", "ot"],
    "MINOR-OT": ["minor ot", "minor operation", "माइनर"],
    "D-1-OTWAIT": ["operation chal", "surgery waiting", "operation ke time", "intezaar", "इंतज़ार", "ऑपरेशन चल", "घरवाले"],
    "C-3-SURGW": ["ward", "wards", "वार्ड", "bed number"],
    "B-2-ENDO": ["endoscopy", "colonoscopy", "doorbeen", "एंडोस्कोपी", "दूरबीन"],
    "B-4-GEN": ["genetic", "brca", "जेनेटिक"],
    "A-4-TOB": ["tobacco", "gutka", "gutkha", "smoking", "bidi", "cigarette", "तंबाकू", "गुटखा", "बीड़ी"],
    "GATE1": ["gate 1", "main gate", "main entrance", "entrance gate", "मेन गेट"],
    "GATE2": ["gate 2", "back gate", "pichla gate", "peeche wala gate", "पिछला गेट", "पीछे वाला गेट"],
}


def _unit_keys(u):
    words = re.findall(r"[a-z0-9-]+|[ऀ-ॿ]+", (u["name"] + " " + u.get("name_hi", "")).lower())
    keys = {w for w in words if w not in NAME_STOP and len(w) >= (4 if w.isascii() else 3)}
    return keys | {s.lower() for s in SYNONYMS.get(u["id"], [])}


DIRECTORY = [(u, _unit_keys(u)) for u in HOSPITAL["units"] + HOSPITAL["campus"]]


def directory_match(t):
    """Best hospital-directory unit for the query text; colloquial synonyms count extra."""
    best, score = None, 0
    for u, keys in DIRECTORY:
        if u.get("public") is False and u["name"].lower() not in t:
            continue                                         # staff-only rooms only when named exactly
        s = 2 * sum(1 for k in keys if hit(k, t))
        if s and any(hit(k.lower(), t) for k in SYNONYMS.get(u["id"], [])):
            s += 1
        if s > score:
            best, score = u, s
    return best, score


UNIT = {u["id"]: u for u in HOSPITAL["units"] + HOSPITAL["campus"]}
HELP_DESK = {"en": "the Helpdesk (Homi Bhabha Building, first floor, Rotunda)", "hi": "हेल्पडेस्क (होमी भाभा बिल्डिंग, पहली मंज़िल, रोटुंडा)"}
CATEGORY_LABEL = {"general": {"en": "general patients", "hi": "जनरल मरीज़"}, "private": {"en": "private patients", "hi": "प्राइवेट मरीज़"}}


def locations(u):
    """[(kind, place id, location)] for a department: general first, then private, then its other listed places."""
    if "block" not in u:
        return []
    out = [("general", u["id"], u)]
    if "private" in u:
        out.append(("private", u["id"] + "@private", u["private"]))
    return out + [("also", f"{u['id']}@{i}", a) for i, a in enumerate(u.get("also", []), 2)]


def location_of(u, category=None):
    """The location to send someone to: the private one for private patients, else the general one."""
    return u["private"] if category == "private" and "private" in u else u


def where_text(loc, lang):
    """'Golden Jubilee Building (GJB), ground floor, counters 54 to 55'."""
    hi = lang == "hi"
    b, f = BLOCKS[loc["block"]], FLOORS[loc["floor"]]
    parts = [b["name_hi"] if hi else b["name"], f["name_hi"] if hi else f["name"].lower()]
    rooms = loc.get("rooms_hi" if hi else "rooms") or ""
    return ", ".join(parts + ([rooms] if rooms else []))


def where_say(u, lang, category=None):
    """Where a department is, every listed place with whom it is for, or just the patient's own if we know their category."""
    hi = lang == "hi"
    if "block" not in u:
        return (u.get("where_hi") or u.get("where") or "") if hi else (u.get("where") or "")
    if category in CATEGORY_LABEL and "private" in u:
        return where_text(location_of(u, category), lang)
    lab = lambda k: CATEGORY_LABEL[k]["hi" if hi else "en"]
    if "private" in u:
        out = f"{lab('general')}: {where_text(u, lang)}; {lab('private')}: {where_text(u['private'], lang)}"
    else:
        out = where_text(u, lang)
    for a in u.get("also", []):
        out += f"; {'और' if hi else 'also'} {where_text(a, lang)}"
    return out


def directory_answer(u, lang, category=None):
    """Full written answer for chat: where (each listed place), how to walk there, timings, what to bring, note."""
    hi = lang == "hi"
    g = lambda f, x=u: (x.get(f + "_hi") or x.get(f)) if hi else x.get(f)
    name = u.get("name_hi") if hi else u["name"]
    end = "।" if hi else "."
    if u.get("unlisted"):                                     # not in the booklet: say so, and who can tell them
        ask = UNIT[u["ask"]]
        return f"{name}: {g('note')}\n➡️ {g('name', ask)}: {where_say(ask, lang, category)}{end}"
    if u.get("dmg"):                                          # an OPD asked about without the cancer type
        return f"{name}: {where_say(u, lang)}\n{g('note')}"
    if "block" not in u:                                      # a gate
        return f"{name}: {g('note')}"
    if u.get("public") is False:
        return (f"{name} ({where_say(u, lang)}) सिर्फ़ स्टाफ़ के लिए है। अगर आपको वहाँ जाने को कहा गया है, तो {HELP_DESK['hi']} पर पूछिए।" if hi else
                f"{name} ({where_say(u, lang)}) is a staff-only area. If you've been asked to go there, please ask at {HELP_DESK['en']}.")
    parts = [f"{name}: {where_say(u, lang, category)}{end}"]
    loc = location_of(u, category)
    if loc.get("path"):
        lead = f"({CATEGORY_LABEL['general']['hi' if hi else 'en']}) " if "private" in u and category not in CATEGORY_LABEL else ""
        parts.append("🚶 " + lead + g("path", loc))
    if u.get("hours"):
        parts.append(f"समय: {g('hours')}।" if hi else f"Timings: {g('hours')}.")
    if u.get("bring"):
        parts.append(f"साथ लाइए: {g('bring')}।" if hi else f"Please bring: {g('bring')}.")
    if u.get("note"):
        parts.append(g("note"))
    return "\n".join(p.replace("।।", "।").replace("..", ".") for p in parts)


# ---------------- Sarvam voice ----------------
def stt(path):
    ext = os.path.splitext(path)[1].lower() or ".wav"
    with open(path, "rb") as f:
        r = requests.post(f"{SARVAM}/speech-to-text", headers={"api-subscription-key": SARVAM_KEY},
                          files={"file": ("audio" + ext, f, mimetypes.guess_type("a" + ext)[0] or "application/octet-stream")},
                          data={"model": STT_MODEL, "language_code": "unknown", "mode": "codemix", "keyterms": json.dumps(KEYTERMS)}, timeout=60)
    r.raise_for_status()
    j = r.json()
    return j.get("transcript", ""), (j.get("language_code") or "")


def voice_for(lang):
    return TTS_VOICE["en" if lang == "en" else "hi"] if TTS_MODEL.startswith("bulbul:v4") else TTS_VOICE["hi"]


def tts(text, lang, codec=None):
    """codec=None gives WAV. codec="opus" gives Ogg/Opus, which WhatsApp plays as a voice note."""
    body = {"text": text[:1400], "target_language_code": LANG_CODE.get(lang, "hi-IN"),
            "model": TTS_MODEL, "speaker": voice_for(lang), "speech_sample_rate": 16000, "enable_preprocessing": True}
    if codec:
        body["output_audio_codec"] = codec
    r = requests.post(f"{SARVAM}/text-to-speech",
                      headers={"api-subscription-key": SARVAM_KEY, "Content-Type": "application/json"},
                      json=body, timeout=60)
    r.raise_for_status()
    j = r.json()
    b64 = (j.get("audios") or j.get("audio"))[0]
    path = tempfile.NamedTemporaryFile(delete=False, suffix=".ogg" if codec == "opus" else f".{codec or 'wav'}").name
    with open(path, "wb") as f:
        f.write(base64.b64decode(b64))
    return path, b64
