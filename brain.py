"""OncoSaathi brain: one conversation engine for the WhatsApp-style chat and our own voice pipeline.

The "rack" (what is fixed vs. what is free):
- FIXED, from data only: rooms, floors, timings, paths (hospital.json); documents and stages (journey.json); stay places
  and phone numbers (stay.json); the patient's tasks and dates (patient_store); safety replies (below).
- FREE: nothing here is generated. The LLM (sarvam-105b) only CLASSIFIES the words into one label; every sentence
  is a template filled from the data. So a room, a number or a date that isn't in the data can't be said.
Safety runs first and can't be overridden: self-harm -> Tele-MANAS 14416; emergency -> nearest nurse / floor help desk
(in hospital) or 108 (at home); medical questions -> doctor / nurse / help desk; fear -> counsellor + Tele-MANAS.

state (one dict per chat session or call): pid, mode ("chat" | "voice"), awaiting, where ("in" | "coming"), place, text.
"""
import asyncio
import datetime as dt
import json
import os
import re
import time

import httpx

import core
import patient_store as store
import route_map

SARVAM = "https://api.sarvam.ai"
LLM_MODEL = os.environ.get("ONCO_LLM_MODEL", "sarvam-105b")
LLM_TIMEOUT = float(os.environ.get("ONCO_LLM_TIMEOUT", "4"))

H = core.HOSPITAL
BLOCKS, FLOORS = core.BLOCKS, core.FLOORS
PLACES = {u["id"]: u for u in H["units"] + H["campus"]}
with open(os.path.join(core.HERE, "stay.json"), encoding="utf-8") as _f:
    STAY = json.load(_f)
with open(os.path.join(core.HERE, "journey.json"), encoding="utf-8") as _f:
    JOURNEY = json.load(_f)
with open(os.path.join(core.HERE, "travel.json"), encoding="utf-8") as _f:
    TRAVEL = json.load(_f)
TELE_MANAS = "14416"

SAFETY = ("SELF_HARM", "EMERGENCY", "CLINICAL", "EMOTIONAL")
LABELS = {  # non-place labels the classifier may return
    "SELF_HARM": "talks about ending their life, self-harm, not wanting to live",
    "EMERGENCY": "urgent danger now: breathing trouble, heavy bleeding, fainting, chest pain, seizure, high fever (esp. during chemo), collapse",
    "CLINICAL": "medical questions: which medicine or dose, symptoms, side effects, what a report means, diagnosis, stage, prognosis, what to eat",
    "EMOTIONAL": "fear, crying, loneliness, hopelessness, feeling overwhelmed",
    "NEXT_STEP": "asks about their OWN next step / what to do today / their appointment, without naming a place",
    "TASK_DONE": "says they have finished a step (test done, report given, paid)",
    "TRAVEL": "asks how to reach the hospital from another city or state: train, flight, bus, station, airport, address",
    "DOCUMENTS": "asks which documents or papers to bring to the hospital in general (first visit)",
    "STAY": "asks where to stay: dharamshala, hostel, guest house, hotel, room near the hospital",
    "FOLLOWUP": "asks about the place discussed just before (what to bring, timings, how to get there) without naming a place",
    "SCAN_GENERIC": "wants a scan but did not say which kind (CT, MRI, X-ray, ultrasound, PET)",
    "OPD_GENERIC": "wants 'the OPD' or 'the doctor' without saying which clinic",
    "GREETING": "only a greeting",
    "BYE": "thanks / goodbye / that's all",
    "NONE": "anything else, or no matching place in the list",
}
ASPECTS = ("where", "bring", "hours", "way", "note", "prep", "cost", "calm", "again", "detail", "G", "1", "2", "3", "4")
COST_RX = r"\b(kharcha|kharch|paisa|paise|free|muft|rupay|rupaye|cost|charge|fees?)\b|खर्चा|खर्च|पैसा|पैसे|मुफ्त|मुफ़्त|रुपया|रुपये|किराया|फ़्री|फ्री"
FEVER_RX = r"\b(fever|bukhar|bukhaar)\b|बुखार"
FEVER_URGENT_RX = r"\b(10[1-5]|chemo|kimo|shiver\w*|chills|kaanp\w*)\b|कीमो|ठंड लग|कंपकंपी|काँप|कांप"
CONCESSION_RX = r"\b(concession|discount|chhoot|chhut|form|certificate)\b|छूट|फ़ॉर्म|फॉर्म|सर्टिफ़िकेट|सर्टिफिकेट|प्रमाण पत्र"
WHATDO_RX = (r"\b(what (do|should) i do there|what happens there|whom (do i|should i) meet|(wahan|vahan|wahaan) (ja ?kar )?kya karna|"
             r"kisse milna|kise milna)\b|जाकर क्या|वहाँ क्या करना|वहां क्या करना|किससे मिलना|किसे मिलना|गेल्यावर काय")
OUTLOOK_RX = r"\b(theek ho|thik ho|bach|survive|khatarnak|dangerous|kitna serious)\w*|ठीक हो|बच |बचेग|खतरनाक|ख़तरनाक|कितना गंभीर"
REPORT_OBJECT_RX = r"(पुरानी|पुराने|old|purani|purane)\s*(रिपोर्ट|report)|(रिपोर्ट|report)\w*\s*(साथ|saath|ले आ|la)"
# Labels that only make sense once we know the hospital. Safety, feelings, documents and chit-chat never wait.
NEEDS_HOSPITAL = {"NAV_NEXT", "FOLLOWUP", "SCAN_GENERIC", "OPD_GENERIC", "NEXT_STEP", "STAY", "TRAVEL", "FLOOR_LIST", "AT_PLACE"}

ASPECT_WORDS = {
    "prep": r"\b(khali pet|khaali pet|empty stomach|fasting|fast|bhookhe|kha ke|kha kar)\b|खाली पेट|भूखे|खा के|खाकर|खा कर",
    "bring": r"\b(bring|carry|documents?|papers?|laana|lana|laaun|laun|lekar|leke|le ke|kagaz|kaagaz)\b|लाना|लाऊँ|लाऊं|लेकर|लेके|साथ में क्या|साथ ला|ले आऊँ|ले आऊं|ला सकती|ला सकता|लानी|कागज़|कागज|डॉक्यूमेंट",
    "hours": r"\b(timings?|time|open|close[sd]?|hours|khula|khulta|khulega|band|kab tak|kitne baje|kab|today|tomorrow|aaj hi|kal bhi)\b|समय|टाइम|खुल|बंद|कितने बजे|कब तक|कब|आज ही|कल भी",
    "way": r"\b(how (do|can) i (go|get|reach)|which lift|rasta|raasta|kaise (jaun|jaaun|jaye|jayen|pahunch))\b|रास्ता|कैसे जा|कैसे पहुँच|कैसे पहुंच|कौन सी लिफ़्ट|कौन सी लिफ्ट",
}
NEXT_STEP_RX = (r"\b(ab|aage|agla|agle|next|aaj)\b.{0,25}\b(kahan|kahaan|kidhar|kya|where|what)\b|"
                r"\bwhat (do|should) i do\b|\bwhere (do|should) i go\b|\bmy (next|appointment)|अगला कदम|आगे क्या|"
                r"(आज|अब).{0,15}(क्या करना|कहाँ जाना|कहां जाना|किधर)|अब क्या|मेरा अपॉइंटमेंट")
DONE_RX = (r"\b(ho gaya|ho gayi|ho gya|kar liya|kar li|de diya|de di|done|finished|completed)\b|हो गया|हो गई|हो गयी|कर लिया|"
           r"कर ली|दे दिया|दे दी")
DOCS_RX = (r"\b(documents?|kagaz|kaagaz|papers?|kya kya (laana|lana|lekar)|konse kagaz)\b|कागज़|कागज|डॉक्यूमेंट|क्या क्या लाना|"
           r"क्या क्या लेकर")
STAY_RX = (r"\b(stay|rehne|rahne|rukne|hotel|lodge|dharamshala|dharmshala|guest ?house|hostel|accommodation)\b|स्टे(?![\u0900-\u097F])|"
           r"(?<![\u0900-\u097F])(रहने|रुकने|होटल|धर्मशाला|लॉज|गेस्ट हाउस|हॉस्टल)(?![\u0900-\u097F])")
TRAVEL_RX = (r"\b(train|railway|flight|plane|airport|station|bus se|address|mumbai (kaise|kese)|kaise (pahunch|aau|aaun|aayen|aaye|jaun|jaau)\w*"
             r".{0,30}(mumbai|tata|hospital))\b|ट्रेन|(?<![\u0900-\u097F])रेल(?![\u0900-\u097F])|फ़्लाइट|फ्लाइट|हवाई|एयरपोर्ट|स्टेशन|मुंबई कैसे|"
             r"(मुंबई|टाटा|अस्पताल|हॉस्पिटल|शहर|गाँव|गांव).{0,25}कैसे (पहुँच|पहुंच|आऊँ|आऊं|जाऊँ|जाऊं)")
QUESTION_RX = (r"\?|\b(kahan|kaha|kahaan|kidhar|kaise|kab|where|how|when)\b|"
               r"(?<![\u0900-\u097F])(कहाँ|कहां|किधर|कैसे|कब)(?![\u0900-\u097F])")
HELP_RX = (r"\b(help|madad|sahayata)\b|(?<![\u0900-\u097F])(मदद|सहायता|हेल्प)(?![\u0900-\u097F])")
CONFIRM_RX = (r"\b(floor|manzil|manjil|wing|room|sahi|theek|right|correct|na)\b|"
              r"(?<![\u0900-\u097F])(मंज़िल|मंजिल|फ्लोर|फ़्लोर|विंग|कमरा|सही|ठीक|ना)(?![\u0900-\u097F])")
FLOOR_WORDS = {"G": r"ground|ग्राउंड|नीचे|neeche|niche", "1": r"\bfirst\b|\b1st\b|pehli|pahli|पहली|पहले (फ्लोर|फ़्लोर|माले|मंज़िल|मंजिल)",
               "2": r"\bsecond\b|\b2nd\b|doosri|dusri|doosre|dusre|दूसरी|दूसरे", "3": r"\bthird\b|\b3rd\b|teesri|tisri|teesre|तीसरी|तीसरे",
               "4": r"\bfourth\b|\b4th\b|chauthi|chauthe|चौथी|चौथे"}
FLOOR_LIST_RX = (r"\bk(?:au|o)ne?\s*-?\s*s[aei]\b|\b(kya kya|kya-kya|kaun kaun|kon kon|kya hai|saare|sare|sab|list|data|"
                 r"what is on|what's on|which department|which departments|what department|what departments)\b|"
                 r"(?<![\u0900-\u097F])(क्या क्या|क्या-क्या|कौन कौन|कौन-कौन|कौन सा|कौन से|कौन सी|कौनसा|कौनसे|क्या है|"
                 r"सारे|सारी|सब|लिस्ट|डेटा)(?![\u0900-\u097F])")
DETAIL_RX = (r"\b(is it (necessary|required|needed)|do (i|we) need|must|zaroori|jaruri|chalti hai|chalegi|milega|milegi|allowed|can (i|we))\b|"
             r"ज़रूरी|जरूरी|चलती है|चलेगी|मिलेगा|मिलेगी|साथ रहना|रह सकते|रह सकती|सकते हैं क्या|होगा क्या|पड़ेगा क्या|है क्या")
DOCTOR_CONNECT_RX = (r"\b(talk to (a |the )?doctor|speak to (a |the )?doctor|connect (me )?(to|with) (a |the )?doctor|doctor se baat)\b|"
                     r"डॉक्टर से (बात|मिलवा|सीधे)|डॉक्टर की बात")
SCHEDULE_RX = r"\b(kab (se|shuru)|when will .{0,20}(start|begin))\b|कब (से|शुरू)|कब होगी|कब होगा"
# Step-by-step walking: "I've reached the first floor / I'm at the lift / which way now?"
NAV_RX = (r"\b(reached|i'?m at|i am at|standing|which way|where now|turn left|turn right|left or right|pahunch gay\w*|pahuch gay\w*|"
          r"aa gay\w*|khada hoon|khadi hoon|ab kahan|ab kidhar|kidhar mud\w*|kahan mud\w*)\b|पहुँच गय|पहुंच गय|पहुँच गई|पहुंच गई|"
          r"आ गया|आ गई|खड़ा हूँ|खड़ी हूँ|खड़ा हूं|खड़ी हूं|अब कहाँ|अब कहां|अब किधर|किधर मुड़|कहाँ मुड़|कहां मुड़|बाएँ या दाएँ|लेफ्ट या राइट")
LIFT_RX = r"\b(lift|elevator)\b|लिफ्ट|लिफ़्ट"
LOBBY_RX = r"\b(gate|lobby|entrance)\b|गेट|लॉबी|मेन गेट"
WHO_RX = (r"\b(who are you|what are you|aap kaun|tum kaun|aap kon|tum kon|kaun ho|kon ho)\b|"
          r"(आप|तुम)\s*(कौन|कोन)\s*(हो|है|हैं)")
NEG_RX = (r"\b(nahi|nahin|nhi)\s+(jaana|jana|jaa)\b|\b(don't|dont|do not) (want to )?go\b|\bnot going\b|"
          r"\b(jaana|jana)\s+(nahi|nahin)\b|नहीं\s*(जाना|जा)(?![\u0900-\u097F])|जाना\s*नहीं")
META_RX = (r"\b(repeat|again and again|baar baar|bar bar|galat|wrong|samajh nahi|samjh nahi|same thing)\b|"
           r"(?<![\u0900-\u097F])(रिपीट|बार-बार|बार बार|ग़लत|गलत|वही)(?![\u0900-\u097F])|समझ नहीं रहे|समझ नहीं रही")
RETURN_RX = (r"\b(wapas|vapas|waapas|back home|return|go back|ghar jaana|ghar jana)\b|वापस|घर जाना|लौट|"
             r"mumbai\s*(se|से)(?![a-z\u0900-\u097F])|मुंबई\s*से|\bfrom mumbai\b")
CHEAP_RX = r"\b(free|muft|sasta|saste|sasti|kam paise|budget|cheap|garib)\b|मुफ़्त|मुफ्त|सस्ता|सस्ते|सस्ती|कम पैसे|गरीब"
CHILD_RX = r"\b(bachch|bachcha|bachche|child|children|beta|beti)\w*|बच्च|बेटा|बेटी"
TATA_RX = r"tata|टाटा|memorial|मेमोरियल|parel|परेल|\btmh\b|\btmc\b"
IN_RX = (r"\b(andar|yahin|yahan|pahunch gay\w*|aa gay\w*|in the hospital|i am here|i'm here|i am in|i'm in|already here|reached)\b|"
         r"\b(me|mein|main)\s*(hoon|hu|hun)\b|(में|मे)\s*(हूँ|हूं|हु)|अंदर|यहीं|यहाँ हूँ|पहुँच गय|पहुंच गय|आ गय")
AT_RX = (r"\b(main|mai|i am|i'm|hum)\b.{0,40}\b(par|pe|pr|at|near|paas|pass)\b|(मैं|हम).{0,40}(पर|पे|के पास|पास)|"
         r"\b(standing|i'm at|i am at)\b|(पर|पे|पास)\s*(खड़ा|खड़ी|खडा|खडी|हूँ|हूं|हैं)")
ACK_RX = (r"^\W*(ok|okay|theek hai|thik hai|achha|accha|got it|thanks|thank you|dhanyavaad|shukriya)\b|^\W*(ठीक है|अच्छा|ओके|हाँ ठीक)|पहुँच रह|पहुंच रह|चला जाता|चली जाती|पूछ लेता|पूछ लेती|पता कर लेता|पता कर लेती|नोट कर लिया|\b(ja raha|ja rahi|jaa raha|"
          r"nikal raha|nikal rahi|samajh gaya|samajh gayi|samajh aa gaya|thank you|thanks)\b|धन्यवाद|शुक्रिया|जा रहा|जा रही|"
          r"निकल रहा|निकल रही|समझ गया|समझ गई|समझ आ गया|सुन लिया")
# heard on WhatsApp calls, 3 Nov 2026: "Yes." / "Yeah." got "sorry, I didn't get that", and "Okay tell me is the radiology
# department" (STT dropped "where") was taken as a plain "okay"
YES_ONLY_RX = r"^\W*(yes|yeah|yep|yup|haan|haan ji|han|ji|ji haan|हाँ|हां|जी|जी हाँ|जी हां|हाँ जी|हां जी)\W*$"
TELL_ME_RX = r"\b(tell me|show me|batao|bataiye|bata do|bataao)\b|बताओ|बताइए|बताइये|बता दो"
FIRST_VISIT_RX = r"\b(pehli baar|pahli baar|first time|first visit|new patient|naya patient)\b|पहली बार|पहली दफ़ा|नया मरीज़|नया मरीज"
APPT_RX = (r"\b(appointment|seedha|sidha|walk in|walk-in|directly|token|bina appointment)\b|अपॉइंटमेंट|अपॉइंटमेंट|सीधा|सीधे|टोकन")
AND_RX = r"\b(aur|and|also|plus)\b|(?<![\u0900-\u097F])(और|तथा)(?![\u0900-\u097F])"
WHERE_NEXT_RX = (r"\b(kahan|kaha|kidhar|where)\b.{0,15}\b(jaun|jaau|jau|jana|jaana|jaaun|go|should i go)\b|\bwhat next\b|"
                 r"\b(aage|ab) kya\b|कहाँ जाऊँ|कहां जाऊं|कहाँ जाना|कहां जाना|किधर जाऊँ|किधर जाना|आगे क्या|अब क्या करूँ|अब कहाँ")
OTHER_NAMES_RX = (r"\b(aiims|apollo|fortis|max|medanta|kem|sion|nair|jj|kokilaben|lilavati|hinduja|manipal|narayana|cmc|pgi|pgimer|"
                  r"safdarjung|rml|aster|care|yashoda|global|wockhardt|breach candy|jaslok)\b|एम्स|अपोलो|फोर्टिस|मेदांता|पीजीआई|सफदरजंग")
OTHER_HOSPITAL_RX = r"\b(hospital|aiims|kem|hospitals)\b|अस्पताल|हॉस्पिटल|हस्पताल|एम्स"
NAME_SAID_RX = r"\b(mera naam|my name is)\b|मेरा नाम"
COMING_RX = (r"\b(aa raha|aa rahi|aa rahe|aane wal\w*|aaunga|aaungi|aaenge|aayenge|coming|will come|going to come|kal aa\w*|ghar|abhi nahi|not yet|"
             r"planning|aana hai|travel)\b|आ रहा|आ रही|आ रहे|आने वाल|आऊँगा|आऊंगा|आऊँगी|आऊंगी|आएँगे|आना है|घर पर|घर से|कल आ|अभी नहीं")
PET_CT_RX = r"(पेट|पीईटी|पी ई टी|\bpet)[\s-]*(सीटी|सी टी|\bct\b)"
DEICTIC = r"\b(wahan|wahaan|vahan|vahaan|udhar|uske|uska|us jagah|there|that place)\b|वहाँ|वहां|उधर|उसके|उसका|उस जगह"
NAME_STOP = {"mera", "meri", "naam", "name", "my", "is", "hai", "hoon", "hu", "hun", "ji", "main", "mai", "i", "am", "i'm", "im",
             "मेरा", "नाम", "है", "हूँ", "हूं", "जी", "मैं", "this", "it's", "its", "namaste", "नमस्ते", "hello", "hi"}


def aspect_from_words(text):
    t = text.lower()
    for a, rx in ASPECT_WORDS.items():
        if re.search(rx, t):
            return a
    return "where"


# ---------------- classifier (labels only, never words for the caller) ----------------
def _place_line(u):
    also = core.SYNONYMS.get(u["id"])
    where = f"{BLOCKS[u['block']]['name']}, {FLOORS[u['floor']]['name']}" if "block" in u else "campus"
    staff = " [staff only]" if u.get("public") is False else ""
    return f"{u['id']}: {u['name']} / {u.get('name_hi', '')} ({where}){staff}" + (f" — also called: {', '.join(also)}" if also else "")


SYSTEM = (
    "You route people talking to a cancer-hospital helper to ONE label. You never answer them yourself.\n"
    "They speak Hindi, Hinglish, English or Marathi; text may be Devanagari or Roman and may contain speech-recognition errors.\n"
    "Return ONLY compact JSON: {\"label\": \"<one label>\", \"aspect\": \"where|bring|hours|way\"}.\n"
    "aspect = what they want to know about a place: where it is (default), what to bring, timings, or how to get there.\n"
    "Safety beats everything: SELF_HARM, then EMERGENCY, then CLINICAL (medical questions even if a place is named).\n"
    "Pick a place id only if they clearly mean that place. Use NONE if unsure; never guess.\n"
    "Hints: 'pet' alone in Hindi often means stomach (पेट): PET-CT only if they mean a scan. 'doorbeen wali jaanch' = endoscopy. "
    "'sikai' / 'sekai' = radiation. 'parcha' = registration. 'khoon ki jaanch' = blood sample collection.\n\n"
    "EXAMPLES:\n"
    "'colonoscopy ke liye kahan jaun' -> {\"label\": \"B-2-ENDO\", \"aspect\": \"where\"}\n"
    "'पी ई टी स्कैन किस मंज़िल पर है' -> {\"label\": \"C-2-PET\", \"aspect\": \"where\"}\n"
    "'papa ka operation ho raha hai, hum kahan intezaar karein' -> {\"label\": \"D-1-OTWAIT\", \"aspect\": \"where\"}\n"
    "'रेडिएशन के लिए कहाँ जाना है' -> {\"label\": \"D-G-RT\", \"aspect\": \"where\"}\n"
    "(place discussed before: C-1-USG) 'wahan kya lekar aana hai' -> {\"label\": \"FOLLOWUP\", \"aspect\": \"bring\"}\n"
    "'aaj mujhe kya karna hai' -> {\"label\": \"NEXT_STEP\", \"aspect\": \"where\"}\n"
    "'blood test ho gaya' -> {\"label\": \"TASK_DONE\", \"aspect\": \"where\"}\n"
    "'Mumbai mein rukne ki sasti jagah' -> {\"label\": \"STAY\", \"aspect\": \"where\"}\n"
    "'pehli baar aa rahe hain, kya kya laana hai' -> {\"label\": \"DOCUMENTS\", \"aspect\": \"where\"}\n"
    "'main Odisha mein hoon, Tata Memorial kaise aaun' -> {\"label\": \"TRAVEL\", \"aspect\": \"where\"}\n"
    "'chemo ke baad ulti aur chakkar' -> {\"label\": \"CLINICAL\", \"aspect\": \"where\"}\n"
    "'मुझे सांस नहीं आ रही' -> {\"label\": \"EMERGENCY\", \"aspect\": \"where\"}\n"
    "'ab jeene ka mann nahi karta' -> {\"label\": \"SELF_HARM\", \"aspect\": \"where\"}\n"
    "'test karwana hai' -> {\"label\": \"NONE\", \"aspect\": \"where\"}\n\n"
    "LABELS:\n" + "\n".join(f"{k}: {v}" for k, v in LABELS.items()) +
    "\n\nPLACE IDS:\n" + "\n".join(_place_line(u) for u in PLACES.values())
)

_client = None


def _http():
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=LLM_TIMEOUT)
    return _client


async def classify(text, last_place=None):
    """Ask the LLM for {label, aspect}. Returns None on any failure or an unknown label."""
    if not core.SARVAM_KEY:
        return None
    ctx = f"Place discussed just before: {last_place} ({PLACES[last_place]['name']})." if last_place in PLACES else "No place discussed yet."
    body = {"model": LLM_MODEL, "temperature": 0, "max_tokens": 40, "reasoning_effort": None,
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": f"{ctx}\nThey said: {text}"}]}
    try:
        for attempt in range(2):
            r = await _http().post(f"{SARVAM}/v1/chat/completions", headers={"api-subscription-key": core.SARVAM_KEY}, json=body)
            if r.status_code not in (429, 500, 502, 503) or attempt:
                break
            await asyncio.sleep(0.4)
        r.raise_for_status()
        raw = (r.json()["choices"][0]["message"].get("content") or "").strip()
        m = re.search(r"\{.*?\}", raw, re.S)
        j = json.loads(m.group(0)) if m else {"label": raw}
        label = str(j.get("label", "")).strip()
        aspect = str(j.get("aspect", "where")).strip().lower()
    except Exception as e:
        print(f"[brain] classifier failed: {e!r}", flush=True)
        return None
    if label not in PLACES and label not in LABELS:
        print(f"[brain] classifier returned unknown label {label!r}; ignored", flush=True)
        return None
    return {"label": label, "aspect": aspect if aspect in ASPECTS else "where"}


# ---------------- decision ----------------
def public_match(t):
    return core.directory_match(t)            # staff-only rooms are already skipped unless named exactly


def rule_decide(text, state):
    """Deterministic fallback when the LLM is unavailable."""
    t = " " + text.lower() + " "
    if re.fullmatch(r"\W*(hi|hello|hey|namaste|namaskar|नमस्ते|नमस्कार|हेलो|हाय)\W*", text.strip().lower()):
        return {"label": "GREETING", "aspect": "where"}
    if re.search(r"\b(thank|thanks|bye|dhanyavaad|dhanyawad|shukriya)\b|धन्यवाद|शुक्रिया", t):
        return {"label": "BYE", "aspect": "where"}
    if re.search(STAY_RX, t):
        return {"label": "STAY", "aspect": "where"}
    unit, score = public_match(t)
    aspect = aspect_from_words(text)
    if unit and score >= 2:
        return {"label": unit["id"], "aspect": aspect}
    if re.search(DOCS_RX, t):
        return {"label": "DOCUMENTS", "aspect": "where"}
    if aspect != "where" and state.get("place"):
        return {"label": "FOLLOWUP", "aspect": aspect}
    return {"label": "NONE", "aspect": "where"}


async def decide(text, state):
    """Return (decision, source). decision = {label, aspect}."""
    t = " " + text.lower() + " "
    if not any(w in t for w in core.SELF_HARM + core.EMERGENCY):
        if re.search(LINE_BUSY_RX, t):
            return {"label": "LINE_BUSY", "aspect": "where"}, "rules-line-busy"
        if re.search(DOCTOR_CONNECT_RX, t):
            return {"label": "DOCTOR_CONNECT", "aspect": "where"}, "rules-doctor"
        if re.search(SCHEDULE_RX, t) and re.search(r"chemo|kimo|कीमो|इलाज|ilaj|treatment|radiation|सिकाई|operation|ऑपरेशन", t):
            return {"label": "SCHEDULE", "aspect": "where"}, "rules-schedule"
    for kind, words in (("SELF_HARM", core.SELF_HARM), ("EMERGENCY", core.EMERGENCY), ("CLINICAL", core.CLINICAL),
                        ("EMOTIONAL", core.EMOTIONAL)):
        if kind == "CLINICAL" and re.search(FEVER_RX, t) and re.search(FEVER_URGENT_RX, t):
            return {"label": "EMERGENCY", "aspect": "fever"}, "rules-safety-fever"    # fever during chemo / with chills / 102+
        if any(w in t for w in words):
            if kind == "CLINICAL" and any(w in t for w in core.EMOTIONAL + ["अकेला", "अकेली", "समझेगा", "समझेगी", "akela", "samjhega"]):
                hits = [w for w in core.CLINICAL if w in t]
                if all(h.strip() in ("दर्द", "dard", "pain") for h in hits):
                    return {"label": "EMOTIONAL", "aspect": "where"}, "rules-safety-heartache"
            if kind == "CLINICAL" and re.search(OUTLOOK_RX, t):
                return {"label": "OUTLOOK", "aspect": "where"}, "rules-safety-outlook"
            if kind == "CLINICAL" and state.get("times_EMERGENCY") and re.search(FEVER_RX, t):
                return {"label": "EMERGENCY", "aspect": "again"}, "rules-safety-fever-again"
            if kind == "EMOTIONAL":
                nm, sc = public_match(t)
                if nm and sc >= 3 and re.search(QUESTION_RX + r"|क्या", t) and not any(w in t for w in core.CLINICAL):
                    return {"label": nm["id"], "aspect": "calm"}, "rules-worried-question"
            return {"label": kind, "aspect": "where"}, "rules-safety"
    named = public_match(t)[0]
    if re.search(OTHER_NAMES_RX, t) and not re.search(TATA_RX, t) and state.get("hospital") != "other":
        state["hospital"] = "other"
        if state.get("pid"):
            store.update(state["pid"], f"brain-{state.get('mode')}", hospital="other")
        if not named or re.search(QUESTION_RX, t) or named:
            return {"label": "HOSP_OTHER", "aspect": "where"}, "rules-other-hospital-named"
    if re.search(TATA_RX, t) and state.get("hospital") != "tata-parel":
        state["hospital"] = "tata-parel"                # named the hospital inside any sentence: remember it, never ask again
        if state.get("pid"):
            store.update(state["pid"], f"brain-{state.get('mode')}", hospital="tata-parel")
    if re.search(YES_ONLY_RX, t) or (re.search(ACK_RX, t) and not re.search(QUESTION_RX + "|" + TELL_ME_RX, t) and len(t.split()) <= 12
                                     and not re.search(NEXT_STEP_RX + "|" + STAY_RX, t)):
        return {"label": "ACK", "aspect": "where"}, "rules-ack"          # "theek hai, main ja raha hoon", "dhanyavaad", "yes"
    if state.get("pid") and re.search(NAME_SAID_RX, t) and extract_name(text):
        return {"label": "NAME_SAID", "aspect": "where"}, "rules-name"
    if re.search(r"\b(which|kis|kaun sa|kaunse) (hospital|aspatal)\b|किस (हॉस्पिटल|अस्पताल|hospital)|कौन से (हॉस्पिटल|अस्पताल|hospital)", t) \
            and not re.search(TATA_RX + "|" + OTHER_NAMES_RX, t):
        return {"label": "ASK_HOSPITAL", "aspect": "where"}, "rules-ask-hospital"
    hs = hospital_statement(t, named)
    if hs:
        return {"label": hs, "aspect": "where"}, "rules-hospital"
    if named and re.search(AT_RX, t) and re.search(WHERE_NEXT_RX, t):
        return {"label": "AT_PLACE", "aspect": named["id"]}, "rules-at-place"     # "I'm at registration, where now?"
    if re.search(CONCESSION_RX, t) and re.search(r"ticket|टिकट|train|ट्रेन|rail|रेल|kiraya|किराय|छूट|chhoot", t):
        return {"label": "CONCESSION", "aspect": "where"}, "rules-concession"
    if state.get("place") in PLACES and re.search(REPORT_OBJECT_RX, t):
        return {"label": state["place"], "aspect": "bring"}, "rules-bring-reports"   # "old reports saath laun?" is about bringing
    if re.search(DETAIL_RX, t) and not re.search(ASPECT_WORDS["bring"] + "|" + ASPECT_WORDS["hours"], t) \
            and not re.search(r"\b(kahan|kahaan|kaha|kidhar|where)\b|कहाँ|कहां|किधर", t):
        target = named if named and public_match(t)[1] >= 3 else (PLACES.get(state.get("place")) if not named else None)
        if target and "block" in target:
            return {"label": target["id"], "aspect": "detail"}, "rules-detail"
    if state.get("place") in PLACES and re.search(WHATDO_RX, t) and not named:
        return {"label": state["place"], "aspect": "note"}, "rules-what-there"
    if (re.search(TRAVEL_RX, t) or travel_state(t)) and not (named and public_match(t)[1] >= 3):
        return {"label": "TRAVEL", "aspect": "where"}, "rules-travel"     # before follow-ups: "train se kaise aaun" is travel
    if re.search(DOCS_RX, t) and not (named and public_match(t)[1] >= 3):
        return {"label": "DOCUMENTS", "aspect": "where"}, "rules-docs"    # "stay mil gaya, par kaagzaat?" -> the documents
    if re.search(STAY_RX, t):
        if named and public_match(t)[1] >= 3 and named["id"] not in ("A-G-SW",):
            return {"label": "STAY_PLUS", "aspect": named["id"]}, "rules-stay-plus"   # "bachchon ka vibhag aur rukne ki jagah"
        return {"label": "STAY", "aspect": "cost" if re.search(COST_RX, t) else "where"}, "rules-stay"
    if re.search(FIRST_VISIT_RX, t) and not named:
        return {"label": "FIRST_VISIT", "aspect": "where"}, "rules-first-visit"
    if state.get("place") in PLACES and "block" in PLACES[state["place"]] and re.search(NAV_RX, t) \
            and (not named or named["id"] == state["place"] or re.search(LIFT_RX + "|" + LOBBY_RX, t)) and not re.search(STAY_RX + "|" + TRAVEL_RX, t):
        return {"label": "NAV_NEXT", "aspect": state["place"]}, "rules-nav-next"    # "lift ke paas hoon, ab kahan?"
    if re.search(META_RX, t) and not named:
        return {"label": "META", "aspect": "where"}, "rules-meta"
    if named and re.search(NEG_RX, t):
        return {"label": "NEGATED", "aspect": "where"}, "rules-negated"
    floor = next((f for f, rx in FLOOR_WORDS.items() if re.search(rx, t)), None)
    if floor and re.search(FLOOR_LIST_RX, t):
        return {"label": "FLOOR_LIST", "aspect": floor}, "rules-floor-list"
    if state.get("place") and re.search(DEICTIC, t) and not named:
        return {"label": "FOLLOWUP", "aspect": aspect_from_words(text)}, "rules-followup"
    if state.get("pid") and re.search(DONE_RX, t):
        if named or re.search(QUESTION_RX, t):
            # "registration ho gaya, ab OPD kahan?": tick the matching step quietly, then answer what comes AFTER "ho gaya"
            m = re.search(DONE_RX, t)
            store.mark_done(state["pid"], hint=t[:m.end()], actor=f"brain-{state.get('mode')}", require_match=True)
            rest = t[m.end():].strip(" ,.।")
            if len(rest) >= 3:
                d, src = await decide(rest, {**state, "pid": None})
                return d, src + "-after-done"
        else:
            return {"label": "TASK_DONE", "aspect": "where"}, "rules-done"
    if state.get("pid") and re.search(NEXT_STEP_RX, t) and not named:
        return {"label": "NEXT_STEP", "aspect": "where"}, "rules-next-step"
    if re.search(STAY_RX, t) and not (named and public_match(t)[1] >= 3):
        return {"label": "STAY", "aspect": "where"}, "rules-stay"
    if re.search(r"(?<![a-z])opd(?![a-z])|ओपीडी", t) and not named:
        return {"label": "OPD_GENERIC", "aspect": "where"}, "rules-opd"
    if (re.search(TRAVEL_RX, t) or travel_state(t)) and not named:
        return {"label": "TRAVEL", "aspect": "where"}, "rules-travel"
    if re.search(DOCS_RX, t) and not named:
        return {"label": "DOCUMENTS", "aspect": "where"}, "rules-docs"
    if named and re.search(APPT_RX, t) and named.get("note"):
        return {"label": named["id"], "aspect": "note"}, "rules-appointment"
    if re.search(COST_RX, t) and state.get("place") in PLACES and state["place"] != "A-G-SW" and not re.search(STAY_RX, t):
        return {"label": "COST", "aspect": state["place"]}, "rules-cost"
    if re.search(ASPECT_WORDS["prep"], t) and not re.search(ASPECT_WORDS["bring"], t):
        target = named if (named and named["id"] != "B-G-REPORT") else PLACES.get(state.get("place"))
        if target:
            return {"label": target["id"], "aspect": "prep"}, "rules-prep"
    if state.get("place") in PLACES and re.search(APPT_RX, t) and not named:
        return {"label": state["place"], "aspect": "note"}, "rules-appointment-followup"
    two = two_places(t)
    if two and set(two) == {"C-1-CT", "C-2-PET"}:
        two = None
    if two and re.search(AT_RX, t):
        def first_pos(uid):
            ps = [t.find(k.lower()) for k in core.SYNONYMS.get(uid, []) if core.hit(k.lower(), t)]
            return min(p for p in ps if p >= 0) if any(p >= 0 for p in ps) else 0
        dest = max(two, key=first_pos)              # the place mentioned later is where they want to go
        return {"label": dest, "aspect": "where"}, "rules-at-going-to"
    if two and re.search(AND_RX, t):
        return {"label": "TWO_PLACES", "aspect": "|".join(two)}, "rules-two-places"
    if named and public_match(t)[1] >= 3:
        # a clear department word ("radiology", "endoscopy", "रजिस्ट्रेशन") is more reliable than the LLM, and faster
        return {"label": named["id"], "aspect": aspect_from_words(text)}, "rules-named"
    llm = await classify(text, state.get("place"))
    if llm and llm["label"] == "SELF_HARM":
        llm["label"] = "EMOTIONAL"            # self-harm wording is caught by keywords above; without it, answer gently
    if llm is None:
        return rule_decide(text, state), "rules"
    words_aspect = aspect_from_words(text)
    if words_aspect != "where":
        llm["aspect"] = words_aspect
    if llm["label"] == "DOCUMENTS" and named:      # "MRI ke liye kya laana hai" is about the MRI, not the first-visit list
        return {"label": named["id"], "aspect": "bring"}, "rules-named-over-documents"
    if llm["label"] == "FOLLOWUP":
        if named:                                   # "Where is the mammography room?" names a place: never "the last place"
            return {"label": named["id"], "aspect": words_aspect}, "rules-named-over-followup"
        if words_aspect == "where" and not re.search(DEICTIC, t):
            return rule_decide(text, state), "rules-not-a-followup"
    if llm["label"] in ("NONE", "SCAN_GENERIC", "OPD_GENERIC"):
        rd = rule_decide(text, state)               # model unsure or generic: a specific rule match may still win
        strong = public_match(t)[1] >= 3            # a colloquial synonym hit, not just one shared word ("blood" bank)
        if (rd["label"] in PLACES and (strong or llm["label"] != "NONE")) or (rd["label"] not in PLACES and rd["label"] != "NONE"):
            return rd, f"rules-after-llm-{llm['label'].lower()}"
    return llm, "llm"


# ---------------- rendering (all facts come from the data files) ----------------
def _clean(s, lang):
    s = re.sub(r"\s*\([^)]*\)", "", s or "").strip()
    s = s.replace("–", " से " if lang == "hi" else " to ")
    s = s.replace("24×7", "चौबीसों घंटे" if lang == "hi" else "24 hours, every day")
    return s.strip(" .।")


def _f(u, field, lang):
    return _clean((u.get(field + "_hi") or u.get(field)) if lang == "hi" else u.get(field), lang)


def _name(u, lang):
    return (u.get("name_hi") or u["name"]) if lang == "hi" else u["name"]


def _wing(u, lang):
    if u["block"] == "CORE":
        return "बीच वाली मुख्य लिफ़्ट के पास" if lang == "hi" else "the centre, by the main lift"
    b = BLOCKS[u["block"]]
    return b["name_hi"].split("·")[0].strip() if lang == "hi" else b["name"]


def _floor(u, lang):
    f = FLOORS[u["floor"]]
    return f["name_hi"] if lang == "hi" else f["name"].lower()


def _lift(u, lang):
    if u["floor"] == "G":
        return ""
    return f"मुख्य लिफ़्ट से {_floor(u, lang)} पर जाइए।" if lang == "hi" else f"Take the main lift to the {_floor(u, lang)}."


def place_short(u, lang):
    """'Wing A, first floor, rooms 101 to 105' — for dates and next steps."""
    if "block" not in u:
        return _name(u, lang)
    return f"{_wing(u, lang)}, {_floor(u, lang)}, {_f(u, 'rooms', lang)}"


def say_place(u, aspect, lang, mode):
    hi = lang == "hi"
    name = _name(u, lang)
    if mode == "chat" and aspect == "where":
        return core.directory_answer(u, lang)
    if "block" not in u:
        return f"{name}: {_f(u, 'note', lang)}।" if hi else f"{name}: {_f(u, 'note', lang)}."
    if u.get("public") is False:
        return (f"{name} सिर्फ़ स्टाफ़ के लिए है। वहाँ जाने को कहा गया हो, तो गेट 1 के हेल्प डेस्क पर पूछिए।" if hi else
                f"{name} is for staff only. If you were asked to go there, please ask at the help desk at Gate 1.")
    if aspect == "bring":
        bring = _f(u, "bring", lang) if u.get("bring") else ""
        fasting = _f(u, "note", lang) if re.search(r"fast", u.get("note") or "", re.I) else ""
        if not bring:
            return (f"{name} के लिए मेरे पास कोई ख़ास सूची नहीं है। अपना UHID कार्ड और पर्ची साथ रखिए।" if hi else
                    f"I don't have a special list for {name}. Please carry your UHID card and your slip.")
        out = f"{name} के लिए साथ लाइए: {bring}।" if hi else f"For {name}, please bring: {bring}."
        return out + (f" {fasting}।" if hi else f" {fasting}.") if fasting else out
    if aspect == "detail":
        if u.get("note"):
            return f"{name}: {_f(u, 'note', lang)}।" if hi else f"{name}: {_f(u, 'note', lang)}."
        return (f"{name} के बारे में यह बात मेरे पास पक्की नहीं है। वहीं के काउंटर पर या गेट 1 के हेल्प डेस्क पर पूछ लीजिए।" if hi else
                f"I don't have that detail for {name}. Please ask at its counter or the help desk inside Gate 1.")
    if aspect == "prep":
        fasting = re.search(r"fast", u.get("note") or "", re.I)
        if fasting:
            return f"{name}: {_f(u, 'note', lang)}।" if hi else f"{name}: {_f(u, 'note', lang)}."
        return (f"{name} के लिए खाली पेट आना है या नहीं, यह आपकी पर्ची पर लिखा होगा; न लिखा हो तो काउंटर पर पूछ लीजिए।" if hi else
                f"Whether to come fasting for {name} is written on your slip; if it isn't, please ask at the counter.")
    if aspect == "calm":
        base = say_place(u, "where", lang, mode)
        return ("घबराइए मत, आप सही जगह पूछ रहे हैं। " if hi else "Don't worry, you're asking the right thing. ") + base + \
               (" बाक़ी बातें वहाँ के डॉक्टर अच्छे से समझा देंगे।" if hi else " The doctors there will explain the rest.")
    if aspect == "note":
        note = _f(u, "note", lang) if u.get("note") else ""
        return ((f"{name}: {note}।" if note else f"{name} के बारे में यह जानकारी मेरे पास नहीं है। गेट 1 के हेल्प डेस्क पर पूछिए।") if hi else
                (f"{name}: {note}." if note else f"I don't have that detail for {name}. Please ask at the help desk inside Gate 1."))
    if aspect == "hours":
        if not u.get("hours"):
            return (f"{name} का समय मेरे पास नहीं है। गेट 1 के हेल्प डेस्क पर पूछिए।" if hi else
                    f"I don't have the timings for {name}. Please ask at the help desk at Gate 1.")
        return f"{name} का समय: {_f(u, 'hours', lang)}।" if hi else f"{name} timings: {_f(u, 'hours', lang)}."
    if aspect == "way" and u.get("path"):
        return (u.get("path_hi") or u["path"]) if hi else u["path"]
    where = (f"{name} {_wing(u, lang)}, {_floor(u, lang)} पर है, {_f(u, 'rooms', lang)}।" if hi else
             f"{name} is in {_wing(u, lang)}, {_floor(u, lang)}, {_f(u, 'rooms', lang)}.")
    if mode == "voice" and len(path_steps(u, lang)) >= 2:
        first = path_steps(u, lang)[0].rstrip("।.")
        return (f"{where} {first}। वहाँ पहुँचकर बताइए, आगे का रास्ता बता दूँगी।" if hi else
                f"{where} {first}. Tell me when you get there and I'll guide you on.")
    return f"{where} {_lift(u, lang)}".strip()


def _when(task, lang):
    d, t = task.get("due_date"), task.get("due_time")
    if not d:
        return ""
    day = dt.date.fromisoformat(d)
    delta = (day - dt.date.today()).days
    hi = lang == "hi"
    word = {0: "आज" if hi else "today", 1: "कल" if hi else "tomorrow", -1: "कल (बीता हुआ)" if hi else "yesterday"}.get(
        delta, day.strftime("%d %b"))
    if t:
        hh, mm = map(int, t.split(":"))
        clock = f"{hh % 12 or 12}:{mm:02d}" if mm else f"{hh % 12 or 12}"
        part = ("सुबह" if hh < 12 else "दोपहर" if hh < 16 else "शाम") if hi else ("am" if hh < 12 else "pm")
        return f"{word} {part} {clock} बजे" if hi else f"{word} at {clock} {part}"
    return word


def _task_line(task, lang):
    title = (task.get("title_hi") or task["title"]) if lang == "hi" else task["title"]
    bits = [title]
    when = _when(task, lang)
    if when:
        bits.append(when)
    u = PLACES.get(task.get("unit_id"))
    if u:
        bits.append(place_short(u, lang))
    return ", ".join(bits)


def say_journey(state, lang, mode, just_done=None):
    hi = lang == "hi"
    pid = state.get("pid")
    if not pid:
        return ("आपका रिकॉर्ड अभी मेरे पास नहीं है। पहले अपना मोबाइल नंबर दर्ज कीजिए।" if hi else
                "I don't have your record yet. Please sign in with your mobile number first.")
    nxt = store.next_tasks(pid, actor=f"brain-{mode}", limit=3 if mode == "chat" else 1)
    lead = ""
    if just_done:
        t = (just_done.get("title_hi") or just_done["title"]) if hi else just_done["title"]
        lead = f"बहुत बढ़िया, {t} हो गया। " if hi else f"Well done, {t} is done. "
    else:
        done = store.done_tasks(pid, actor=f"brain-{mode}", limit=1)
        if done:
            t = (done[0].get("title_hi") or done[0]["title"]) if hi else done[0]["title"]
            lead = f"आपका {t} हो चुका है। " if hi else f"Your {t} is done. "
    if not nxt:
        return lead + ("अभी आपका कोई अगला काम मेरे पास दर्ज नहीं है। डॉक्टर की पर्ची की फ़ोटो चैट में भेजिए, मैं बता दूँगी कहाँ जाना है।" if hi else
                       "I don't have another step for you yet. Send a photo of the doctor's slip in the chat and I'll tell you where to go.")
    if state.get("place") is None and nxt[0].get("unit_id") in PLACES:
        state["place"] = nxt[0]["unit_id"]
    first = _task_line(nxt[0], lang)
    if mode == "voice":
        return lead + (f"अगला कदम: {first}।" if hi else f"Next step: {first}.")
    lines = [lead + ("आपके अगले कदम:" if hi else "Your next steps:")]
    lines += [f"{i}. {_task_line(t, lang)}" for i, t in enumerate(nxt, 1)]
    return "\n".join(lines)


def say_documents(lang, mode):
    hi = lang == "hi"
    k = 1 if hi else 0
    always = [d[k] for d in JOURNEY["documents"]["always"]]
    extra = [d[k] for d in JOURNEY["documents"]["if_applicable"]]
    if mode == "voice":
        return ("पहली बार आते समय साथ लाइए: " + ", ".join(always[:5]) + "। पूरी सूची चैट में भी है।" if hi else
                "For your first visit, bring: " + ", ".join(always[:5]) + ". The full list is in the chat.")
    head = "अस्पताल आते समय ये साथ लाइए:" if hi else "Bring these when you come to the hospital:"
    more = "अगर हो तो ये भी:" if hi else "If you have them, also bring:"
    return "\n".join([head] + [f"• {d}" for d in always] + ["", more] + [f"• {d}" for d in extra])


def say_floor(fid, lang, mode):
    """Everything public on one floor, wing by wing, from hospital.json."""
    hi = lang == "hi"
    us = [u for u in H["units"] if u["floor"] == fid and u.get("public", True)]
    fname = FLOORS[fid]["name_hi"] if hi else FLOORS[fid]["name"].lower()
    if mode == "voice":
        names = ", ".join(_name(u, lang) for u in us)
        return (f"{fname} पर ये हैं: {names}। किसका रास्ता बताऊँ?" if hi else f"On the {fname}: {names}. Which one should I guide you to?")
    lines = [f"{fname} पर:" if hi else f"On the {fname}:"]
    lines += [f"• {_name(u, lang)}: {_wing(u, lang)}, {_f(u, 'rooms', lang)}" for u in us]
    return "\n".join(lines)


def two_places(t):
    """Two different departments clearly named in one sentence ('where to wait and where to get food')."""
    hits = []
    for u, keys in core.DIRECTORY:
        if u.get("public") is False or "block" not in u:
            continue
        syn = [k.lower() for k in core.SYNONYMS.get(u["id"], [])]
        if any(core.hit(k, t) for k in syn):
            hits.append(u["id"])
    return hits[:2] if len(hits) >= 2 else None


def path_steps(u, lang):
    """The walking path as separate steps: lift / turn / door. Split on sentence ends and on ', then' / ', फिर'."""
    text = (u.get("path_hi") or u.get("path")) if lang == "hi" else u.get("path")
    if not text:
        return []
    parts = re.split(r"(?<=[।.])\s+|,\s*(?=फिर |then )", text)
    steps = [p.strip(" ,") for p in parts if p.strip(" ,।.")]
    return [p[0].upper() + p[1:] if p and p[0].isascii() else p for p in steps]


def say_nav(u, state, lang, text):
    """Next step of the walk, from where the caller says they are."""
    hi = lang == "hi"
    steps = path_steps(u, lang)
    if not steps:
        return say_place(u, "where", lang, "voice")
    t = " " + text.lower() + " "
    lift_i = next((i for i, st in enumerate(steps) if re.search(LIFT_RX, st.lower())), None)
    floor_said = next((f for f, rx in FLOOR_WORDS.items() if re.search(rx, t)), None)
    if re.search(LIFT_RX, t) or (floor_said and floor_said == u.get("floor") and floor_said != "G"):
        idx = (lift_i + 1) if lift_i is not None else (state.get("path_step") or 0) + 1   # at the lift / on the right floor
    elif re.search(LOBBY_RX, t):
        idx = 0
    else:
        idx = (state.get("path_step") if state.get("path_step") is not None else -1) + 1
    if idx >= len(steps):                                             # already at the end of the path
        state["path_step"] = len(steps) - 1
        return (f"आप लगभग पहुँच गए हैं: {_f(u, 'rooms', lang)} वहीं है। न दिखे तो पास के किसी स्टाफ़ से पूछ लीजिए।" if hi else
                f"You're nearly there: {_f(u, 'rooms', lang)} is right there. If you can't see it, ask any staff member nearby.")
    state["path_step"] = idx
    step = steps[idx]
    if hi and step.startswith("फिर "):
        step = step[4:]
    if not hi and step.lower().startswith("then "):
        step = step[5:6].upper() + step[6:]
    tail = "" if idx == len(steps) - 1 else (" वहाँ पहुँचकर बताइए।" if hi else " Tell me when you get there.")
    return step.rstrip("।.") + ("।" if hi else ".") + tail


# ---------------- walkthrough map: where the patient is standing, and the route from there ----------------
_NOT_TIME = r"(?!\s*(?:pehle|pahle|baad|पहले|बाद))"
_AT_AFTER = (r"\s*(?:(?:par|pe|pr|पर|पे|ke\s+paas|ke\s+pass|के\s+पास)(?![a-zऀ-ॿ])(?!\s*(?:jana|jaana|jaun|जाना|जाऊँ|जाऊं))|"
             r"(?:mein|में)\s*(?:hoon|hun|hu|हूँ|हूं))")   # "X par jana hai" is where they want to go, not where they are
_SE_AFTER = r"\s*(?:se|से)(?![a-zऀ-ॿ])" + _NOT_TIME
_FROM_BEFORE = r"\bfrom\s+(?:the\s+)?"
_AT_BEFORE = r"\b(?:at|near|outside)\s+(?:the\s+)?"
_GO_RX = r"\b(jaana|jana|jaun|jaau|jaaun|kaise|kahan|kaha|kidhar|go|get|reach)\b|जाना|जाऊँ|जाऊं|कैसे|कहाँ|कहां|किधर"
_ARRIVED_RX = r"\b(reached|pahunch gay\w*|pahuch gay\w*|aa gay\w*)\b|पहुँच गय|पहुंच गय|पहुँच गई|पहुंच गई|आ गया|आ गई"
_I_AM_RX = AT_RX + "|" + _ARRIVED_RX + r"|\b(hoon|hun|khada|khadi)\b|(?<![ऀ-ॿ])(हूँ|हूं|खड़ा|खड़ी)(?![ऀ-ॿ])"


def _start_markers(key, t):
    """Is this place named as where they are ('at'), where they set off from ('from'), or neither (None)?"""
    k = re.escape(key)
    if re.search(_I_AM_RX, t) and (re.search(k + _AT_AFTER, t) or re.search(_AT_BEFORE + k, t)):
        return "at"
    if re.search(_FROM_BEFORE + k, t) or (re.search(k + _SE_AFTER, t) and re.search(_GO_RX, t)):
        return "from"
    return None


def spoken_start(text, dest=None):
    """Where the patient says they are standing, as a route_map place, or None. Returns (place, 'at' or 'from')."""
    t = " " + text.lower() + " "
    m = re.search(r"\bgate\s*(1|2|one|two|ek|do)\b|गेट\s*(1|2|एक|दो)", t)
    if m:
        place = "GATE2" if (m.group(1) or m.group(2)) in ("2", "two", "do", "दो") else "GATE1"
        if _start_markers(m.group(0), t):
            return place, _start_markers(m.group(0), t)
    for key in ("main gate", "entrance", "lobby", "मेन गेट", "लॉबी"):
        if key in t and _start_markers(key, t):
            return "GATE1", _start_markers(key, t)
    floor = next((f for f, rx in FLOOR_WORDS.items() if re.search(rx, t)), None)
    lift = re.search(LIFT_RX, t)
    if lift and _start_markers(lift.group(0), t):
        return f"LIFT-{floor or (route_map.floor_of(dest) if dest else 'G')}", _start_markers(lift.group(0), t)
    keys = sorted(((k.lower(), uid) for uid, ks in core.SYNONYMS.items() for k in ks if route_map.is_place(uid)), key=lambda x: -len(x[0]))
    named = [(uid, _start_markers(k, t)) for k, uid in keys if core.hit(k, t) and _start_markers(k, t)]
    for uid, kind in named:
        if uid != dest:
            return uid, kind
    if named:
        return named[0]
    if floor and re.search(_I_AM_RX, t) and not re.search(QUESTION_RX, t):
        return f"LIFT-{floor}", "at"                     # "second floor par hoon": the lift lobby is the one point we know
    return None


def place_name(place, lang):
    hi = lang == "hi"
    if place == "GATE1":
        return "गेट 1" if hi else "Gate 1"
    if place == "GATE2":
        return "गेट 2" if hi else "Gate 2"
    if place.startswith("LIFT-"):
        return "मुख्य लिफ़्ट" if hi else "the main lift"
    return _name(PLACES[place], lang)


def walk_text(start, dest, lang):
    """The same walk the map draws, in words. No 'left' or 'right' out of a door: those depend on which way you face."""
    hi = lang == "hi"
    u = PLACES[dest]
    trip = route_map.legs(start, dest)
    parts = []
    if len(trip) > 1 or (trip[0]["floor"] != route_map.floor_of(start)):
        parts.append(f"{place_name(start, lang)} से कॉरिडोर में होकर मुख्य लिफ़्ट तक जाइए और {_floor(u, lang)} पर आइए।" if hi else
                     f"From {place_name(start, lang)}, walk along the corridor to the main lift and go to the {_floor(u, lang)}.")
    elif start.startswith("LIFT-"):
        parts.append(f"लिफ़्ट से निकलकर कॉरिडोर में आइए।" if hi else "Leave the lift and step into the corridor.")
    if len(trip) > 1:
        lead = "लिफ़्ट से निकलकर" if hi else "Leave the lift and"
    elif start.startswith("LIFT-"):
        lead = "फिर" if hi else "Then"
    else:
        lead = f"{place_name(start, lang)} से" if hi else f"From {place_name(start, lang)},"
    hand = route_map.side(start, dest)
    rooms = _f(u, "rooms", lang)
    if hi:
        at = {"left": f"{rooms} आपके बाएँ हाथ पर है।", "right": f"{rooms} आपके दाएँ हाथ पर है।", "ahead": f"{rooms} सामने है।"}[hand]
        parts.append(f"{lead} कॉरिडोर में {_wing(u, lang)} की तरफ़ चलिए। {at}")
    else:
        at = {"left": f"{rooms} is on your left.", "right": f"{rooms} is on your right.", "ahead": f"{rooms} is straight ahead."}[hand]
        parts.append(f"{lead} walk along the corridor towards {_wing(u, lang)}. {at}" if lead.endswith(",") else
                     f"{lead} walk along the corridor towards {_wing(u, lang)}. {at}")
    return f"{_name(u, lang)}: " + " ".join(parts)


def map_for(start, dest, lang, told):
    """What the chat needs to show the map: the image link and a short caption saying where the line starts."""
    hi = lang == "hi"
    where = place_name(start, lang)
    if told:
        caption = f"📍 रास्ता: {where} से {_name(PLACES[dest], lang)} तक।" if hi else f"📍 Your route from {where} to {_name(PLACES[dest], lang)}."
    else:
        caption = (f"📍 मैंने {where} से रास्ता बनाया है। आप कहीं और खड़े हैं तो बताइए, जैसे \"मैं फ़ार्मेसी पर हूँ\"।" if hi else
                   f"📍 I started this route at {where}. If you're standing somewhere else, tell me, like \"I'm at the pharmacy\".")
    return {"from": start, "to": dest, "url": f"/map/route.jpg?from={start}&to={dest}&v={route_map.MAP_VERSION}", "caption": caption}


def walk_dest(decision, state):
    """The room this answer sends them to, if any. Safety answers never carry a map."""
    label, aspect = decision["label"], decision["aspect"]
    if label in SAFETY:
        return None
    dest = None
    if label in PLACES and aspect in ("where", "way", "calm", "again"):
        dest = label
    elif label == "FOLLOWUP" and aspect in ("where", "way"):
        dest = state.get("place")
    elif label == "TWO_PLACES":
        dest = aspect.split("|")[0]
    elif label in ("STAY_PLUS", "NAV_NEXT"):
        dest = aspect
    elif label == "FIRST_VISIT":
        dest = "A-G-REG"
    elif label in ("NEXT_STEP", "TASK_DONE"):
        dest = state.get("place")
    return dest if route_map.is_place(dest) and dest not in route_map.GATE_X else None


def say_after(uid, state, lang, mode):
    """'I'm at X, where should I go now?': their own next step if we have one, else the usual next step after X."""
    hi = lang == "hi"
    if state.get("pid") and store.next_tasks(state["pid"], actor=f"brain-{mode}", limit=1):
        return say_journey(state, lang, mode)
    opd, report = PLACES["A-1-MED"], PLACES["B-G-REPORT"]
    nxt = {
        "A-G-REG": ("रजिस्ट्रेशन के बाद अपना UHID कार्ड लेकर ओपीडी जाइए: विंग A, पहली मंज़िल, मुख्य लिफ़्ट से। कमरा नंबर आपकी ओपीडी पर्ची पर होगा। "
                    "रजिस्ट्रेशन अभी बाकी हो तो पहले काउंटर 1 से 6 पर कराइए।",
                    "After registration, take your UHID card to the OPD: Wing A, first floor, by the main lift. Your room number is on your OPD slip. "
                    "If you haven't registered yet, do that first at counters 1 to 6."),
        "A-G-BILL": ("पैसे जमा करके रसीद लीजिए, फिर उस जाँच की जगह जाइए जो पर्ची पर लिखी है। जाँच का नाम बताइए या पर्ची की फ़ोटो भेजिए, मैं रास्ता बता दूँगी।",
                     "After paying, keep the receipt and go to the test written on your slip. Tell me the test name or send a photo of the slip and I'll guide you."),
        "B-G-SAMPLE": (f"सैंपल देने के बाद रिपोर्ट {place_short(report, lang)} के रिपोर्ट काउंटर से मिलती है। {_f(report, 'note', lang)}।",
                       f"After the sample, collect the report at the report counter, {place_short(report, lang)}. {_f(report, 'note', lang)}."),
        "A-G-HELP": ("नए मरीज़ हैं तो पहले रजिस्ट्रेशन: मेन लॉबी, काउंटर 1 से 6। पहले से रजिस्टर हैं तो बताइए किस विभाग जाना है, या पर्ची की फ़ोटो भेजिए।",
                     "If you're a new patient, register first at the main lobby, counters 1 to 6. If you're already registered, tell me the department or send a photo of your slip."),
    }
    if uid in nxt:
        return nxt[uid][0 if hi else 1]
    return ("आगे कहाँ जाना है, यह आपकी डॉक्टर की पर्ची पर लिखा होगा। विभाग का नाम बताइए या पर्ची की फ़ोटो भेजिए, मैं रास्ता बता दूँगी।" if hi else
            "Where to go next is written on your doctor's slip. Tell me the department or send a photo of the slip and I'll guide you.")


def hospital_statement(t, named):
    """'Main Tata Memorial mein hoon' / 'Tata Memorial jaana hai' / 'AIIMS mein hoon': where they are, said in passing.
    Returns HOSP_TATA_IN, HOSP_TATA_COMING, HOSP_TATA, HOSP_OTHER, or None when it's really a question about something else."""
    if named and public_match(t)[1] >= 3:
        return None                                   # "Tata Memorial mein CT kahan hai?" is about the CT
    if re.search(TATA_RX, t):
        if travel_state(t) or re.search(COMING_RX, t) or re.search(r"(जाना है|आना है|jaana hai|jana hai|aana hai)", t):
            return "HOSP_TATA_COMING"
        if re.search(IN_RX, t):
            return "HOSP_TATA_IN"
        if len(t.split()) <= 8 and not re.search(QUESTION_RX, t):
            return "HOSP_TATA"
        return None
    if re.search(OTHER_NAMES_RX, t) and re.search(IN_RX + "|" + COMING_RX, t) and not named and not re.search(QUESTION_RX, t):
        return "HOSP_OTHER"
    return None


def travel_state(t):
    for key, info in TRAVEL["states"].items():
        if any(n in t for n in info["names"]):
            return key
    return None


def say_travel(text, lang, mode):
    hi = lang == "hi"
    if re.search(RETURN_RX, " " + text.lower() + " "):          # going home: hospital -> Dadar -> home state
        st_key = travel_state(" " + text.lower() + " ")
        st_info = TRAVEL["states"].get(st_key or "")
        parts = [TRAVEL["return"]["hi" if hi else "en"]]
        if st_info and st_info.get("return"):
            parts.insert(0, st_info["return"]["hi" if hi else "en"])
        parts.append(TRAVEL["check_hi"] if hi else TRAVEL["check"])
        return " ".join(parts)
    k = (lambda f: TRAVEL[f + "_hi"]) if hi else (lambda f: TRAVEL[f])
    st = TRAVEL["states"].get(travel_state(" " + text.lower() + " ") or "")
    sk = (lambda f: st[f + "_hi"]) if hi else (lambda f: st[f])
    if mode == "voice":
        short = ("कैंसर मरीज़ों को रेल किराए में छूट भी मिलती है, अस्पताल के सर्टिफ़िकेट से।" if hi else
                 "Cancer patients also get a rail fare concession, with a certificate from their hospital.")
        wants_help = re.search(r"madad|help|मदद|सहायता|rehne|stay|रहने", " " + text.lower() + " ")
        parts = ([sk("train")] if st else []) + [k("by_train"), short] + ([sk("help")] if st and wants_help else [])
        return " ".join(parts)
    lines = [("📍 " + TRAVEL["address_hi"]) if hi else ("📍 " + TRAVEL["address"])]
    if st:
        lines.append("🚆 " + sk("train"))
    lines += ["🚆 " + k("by_train"), "🚉 " + k("local"), "✈️ " + k("by_air"), "🎫 " + k("concession")]
    if st:
        lines.append("🤝 " + sk("help"))
    lines += ["", k("check"), ("अस्पताल: " if hi else "Hospital: ") + TRAVEL["phone"]]
    return "\n".join(lines)


def say_stay(text, lang, mode, page=0, costs=False):
    hi = lang == "hi"
    places = STAY["places"]
    t = text.lower()
    kids = {"IMPACCT", "HELPING-HANDS", "STJUDE"}
    if re.search(CHILD_RX, t):
        places = [p for p in places if p["id"] in kids] + [p for p in places if p["id"] not in kids]
    else:
        places = [p for p in places if p["id"] not in kids]
    if re.search(CHEAP_RX, t):
        order = {"free": 0, "under_300": 1, "unknown": 2, "300_1000": 3}
        places = sorted(places, key=lambda p: order.get(p["band"], 9))
    note = STAY["note_hi"] if hi else STAY["note"]
    if costs:                                         # "kya kharcha lagega / muft hai?": the costs as listed, free ones first
        order = {"free": 0, "under_300": 1, "300_1000": 2, "unknown": 3}
        picks = sorted(places, key=lambda p: order.get(p["band"], 9))[:3]
        parts = [f"{p['name']}: {p['cost']}" for p in picks]
        return (("खर्चा जगह के हिसाब से है: " + "; ".join(parts) + "। फ़ोन का पैसा न हो तो अस्पताल में सामाजिक कार्य डेस्क (विंग A, काउंटर 12–13) मुफ़्त में मदद करता है।") if hi else
                ("It depends on the place: " + "; ".join(parts) + ". If you can't afford a phone call, the social work desk at the hospital (Wing A, counters 12–13) helps for free."))
    if mode == "voice":
        picks = places[2 * page:2 * page + 2] or places[:2]
        parts = [f"{p['name']}, {p['area']}, नंबर {p['phones'][0]}" if hi else f"{p['name']} in {p['area']}, number {p['phones'][0]}" for p in picks]
        return ("पास में रहने के लिए: " + "; ".join(parts) + "। पहले फ़ोन करके पक्का कर लीजिए, मैं बुकिंग नहीं करती। पूरी सूची चैट में है।" if hi else
                "Places to stay nearby: " + "; ".join(parts) + ". Please call first to confirm; I don't make bookings. The full list is in the chat.")
    lines = ["टाटा मेमोरियल के पास रहने की जगहें:" if hi else "Places to stay near Tata Memorial:"]
    for p in places[:6]:
        lines.append(f"• {p['name']} ({p['area']}): {', '.join(p['phones'])}. {p['cost']}.")
    lines += ["", note, "सामाजिक कार्य डेस्क (विंग A, काउंटर 12–13) भी मदद कर सकता है।" if hi else
              "The social work desk (Wing A, counters 12–13) can also help you find a place."]
    return "\n".join(lines)


VOICE_SAFETY = {
    "SELF_HARM": {"en": "I'm really glad you told me. Your life matters. Please stay with a family member or a staff member right now, "
                        "and call Tele-MANAS on 14416. It's free, 24 hours, in your language.",
                  "hi": "आपने बताया, बहुत अच्छा किया। आपकी ज़िंदगी बहुत क़ीमती है। अभी किसी अपने या स्टाफ़ के पास रहिए, "
                        "और टेली-मानस 14416 पर कॉल कीजिए। यह मुफ़्त है, चौबीसों घंटे, आपकी भाषा में।"},
    "CLINICAL": {"en": "That's a question for your doctor or nurse; I can't advise on medicines or treatment. "
                       "Please ask the nurse or help desk on your floor, or the help desk inside Gate 1.",
                 "hi": "यह सवाल डॉक्टर या नर्स का है, मैं दवा या इलाज की सलाह नहीं दे सकती। "
                       "अपनी मंज़िल की नर्स या हेल्प डेस्क से पूछिए, या गेट 1 के अंदर हेल्प डेस्क से।"},
    "EMOTIONAL": {"en": "I understand, this is a hard time, and you are not alone. Our counsellors in Wing A, fourth floor, rooms 401 to 403, "
                        "can talk with you today. Any time, day or night, you can also call Tele-MANAS on 14416, free.",
                  "hi": "मैं समझ सकती हूँ, यह मुश्किल समय है, आप अकेले नहीं हैं। विंग A, चौथी मंज़िल, कमरा 401 से 403 में काउंसलर आज ही आपसे बात कर सकते हैं। "
                        "दिन हो या रात, टेली-मानस 14416 पर भी मुफ़्त बात कर सकते हैं।"},
}
EMERGENCY = {
    "in": {"en": "This could be an emergency. Tell the nearest nurse or the help desk on this floor right now.",
           "hi": "यह इमरजेंसी हो सकती है। अभी तुरंत पास की नर्स को या इसी मंज़िल के हेल्प डेस्क को बताइए।"},
    "coming": {"en": "This could be an emergency. Call 108 right now, or go to the nearest emergency.",
               "hi": "यह इमरजेंसी हो सकती है। अभी 108 पर कॉल कीजिए या सबसे पास की इमरजेंसी में जाइए।"},
    None: {"en": "This could be an emergency. If you're in the hospital, tell the nearest nurse or floor help desk right now. If you're at home, call 108.",
           "hi": "यह इमरजेंसी हो सकती है। अस्पताल में हैं तो अभी पास की नर्स या मंज़िल के हेल्प डेस्क को बताइए। घर पर हैं तो 108 पर कॉल कीजिए।"},
}
AGAIN = {
    "EMOTIONAL": {"en": "I'm here and I'm listening. It's okay to feel this way. Would you like me to guide you to the counsellors now? "
                        "And Tele-MANAS 14416 is always there, free.",
                  "hi": "मैं आपकी बात सुन रही हूँ। ऐसा महसूस करना बिल्कुल स्वाभाविक है। क्या मैं आपको अभी काउंसलर तक का रास्ता बताऊँ? "
                        "और टेली-मानस 14416 हमेशा मुफ़्त में बात करने के लिए है।"},
    "CLINICAL": {"en": "I understand it's urgent. Changing a medicine or its dose on your own can be risky, so please tell the nearest nurse right now. "
                       "If the pain is unbearable, go to Emergency; at home, call 108.",
                 "hi": "मैं समझ रही हूँ कि बात ज़रूरी है। दवा या उसकी मात्रा ख़ुद बदलना ख़तरनाक हो सकता है, इसलिए अभी पास की नर्स को बताइए। "
                       "दर्द सहन न हो तो इमरजेंसी जाइए, घर पर हों तो 108 पर कॉल कीजिए।"},
    "SELF_HARM": {"en": "You matter, and you don't have to face this alone. Please call Tele-MANAS on 14416 now, or stay with someone near you.",
                  "hi": "आप बहुत क़ीमती हैं, और आपको यह अकेले नहीं सहना है। अभी टेली-मानस 14416 पर कॉल कीजिए, या किसी अपने के पास रहिए।"},
}
COMFORT = {   # rotating fixed lines for someone who keeps needing comfort: warm, never medical, never a promise
    "hi": ["मैं यहीं हूँ, आपकी बात सुन रही हूँ। एक गहरी साँस लीजिए। आप अपनी माँ के लिए जो कर रहे हैं, वह बहुत हिम्मत की बात है।",
           "आप अकेले नहीं हैं। बहुत से परिवार यही महसूस करते हैं, और धीरे-धीरे रास्ता मिलता है। आज बस अगला एक कदम सोचिए।",
           "अपना ख़याल रखना भी ज़रूरी है। थोड़ा पानी पीजिए, कुछ खा लीजिए। जब मन करे, मुझसे कुछ भी पूछिए।"],
    "en": ["I'm here and listening. Take a slow, deep breath. What you're doing for your mother takes real courage.",
           "You're not alone. Many families feel exactly this, and a way forward does come. Just think about the next single step today.",
           "Looking after yourself matters too. Have some water, eat something. Ask me anything whenever you're ready."],
}
LINE_BUSY_RX = r"(phone|फोन|फ़ोन|call|कॉल).{0,30}(nahi utha|नहीं उठा|not answer|no answer|busy|व्यस्त|नहीं लग)"
UNKNOWN = {"en": "Sorry, I'm not sure about that. Please ask at the help desk inside Gate 1, on the left.",
           "hi": "माफ़ कीजिए, इसकी पक्की जानकारी मेरे पास नहीं है। गेट 1 के अंदर बाईं ओर हेल्प डेस्क पर पूछिए।"}
TELE_ACTION = [{"label": "📞 Tele-MANAS 14416", "tel": TELE_MANAS}]


def say(decision, state, lang, mode, text):
    """Turn a decision into words. Returns (reply, actions)."""
    label, aspect = decision["label"], decision["aspect"]
    hi = lang == "hi"
    if label == "EMERGENCY":
        state["times_EMERGENCY"] = state.get("times_EMERGENCY", 0) + 1
        if aspect == "fever":
            where = state.get("where")
            return (("कीमो के दौरान या कंपकंपी के साथ तेज़ बुखार इमरजेंसी है, इंतज़ार मत कीजिए। " +
                     ("अभी पास की नर्स को बताइए।" if where == "in" else "अभी 108 पर कॉल कीजिए या सबसे पास की इमरजेंसी जाइए।")) if hi else
                    ("A high fever during chemo or with shivering is an emergency; don't wait. " +
                     ("Tell the nearest nurse now." if where == "in" else "Call 108 now or go to the nearest emergency."))), []
        if aspect == "again":
            return ("कीमो के दौरान तेज़ बुखार में इंतज़ार मत कीजिए, यह जल्दी देखा जाना चाहिए। अभी 108 पर कॉल कीजिए या सबसे पास की इमरजेंसी जाइए।" if hi else
                    "During chemo, a high fever shouldn't wait; it needs to be seen quickly. Call 108 now or go to the nearest emergency."), []
        return EMERGENCY[state.get("where")][lang], []
    if label in ("SELF_HARM", "EMOTIONAL", "CLINICAL"):
        n = state[f"times_{label}"] = state.get(f"times_{label}", 0) + 1
        if label == "EMOTIONAL" and n >= 3:
            return COMFORT[lang][(n - 3) % 3], TELE_ACTION
        if n >= 2 and label in AGAIN:                 # second time: shorter, warmer, not a copy of the first answer
            return AGAIN[label][lang], (TELE_ACTION if label != "CLINICAL" else [])
        return VOICE_SAFETY[label][lang], (TELE_ACTION if label != "CLINICAL" else [])
    if label == "TASK_DONE":
        done = store.mark_done(state["pid"], hint=text, actor=f"brain-{mode}") if state.get("pid") else None
        if not done:
            return ("अच्छा! आगे कुछ और जानना हो तो बताइए।" if hi else "Good. Tell me if you need anything else."), []
        return say_journey(state, lang, mode, just_done=done), []
    if label == "NEXT_STEP":
        return say_journey(state, lang, mode), []
    if label == "DOCUMENTS":
        return say_documents(lang, mode), []
    if label == "STAY":
        again = state.get("last_label") == "STAY"
        if aspect == "cost" or (again and re.search(COST_RX, " " + text.lower() + " ")):
            return say_stay(text, lang, mode, costs=True), []
        page = state.get("stay_page", 0) + 1 if again else 0       # asked again: the next two places, not the same two
        state["stay_page"] = page
        return say_stay(text, lang, mode, page=page), []
    if label == "STAY_PLUS":
        u = PLACES[aspect]
        state["place"] = u["id"]
        return say_place(u, "where", lang, "voice") + " " + say_stay(text, lang, mode), []
    if label == "TRAVEL":
        return say_travel(text, lang, mode), []
    if label == "LINE_BUSY":
        return ("कभी-कभी लाइन व्यस्त रहती है, थोड़ी देर में फिर कोशिश कीजिए। अस्पताल में हैं तो विंग A, चौथी मंज़िल पर काउंसलर से सीधे मिल सकते हैं, "
                "और तब तक मैं यहीं हूँ, आप मुझसे बात कर सकते हैं।" if hi else
                "Sometimes the line is busy; please try again in a little while. If you're at the hospital, you can see the counsellors directly on Wing A, "
                "fourth floor. Until then I'm here, you can talk to me."), TELE_ACTION
    if label == "DOCTOR_CONNECT":
        h = PLACES["A-G-HELP"]
        return (f"मैं सीधे डॉक्टर से नहीं जोड़ सकती। हेल्प डेस्क ({place_short(h, lang)}) की नर्स नेविगेटर आपकी बात डॉक्टर तक पहुँचाती हैं, "
                "और ओपीडी में आप डॉक्टर से सीधे पूछ सकते हैं।" if hi else
                f"I can't connect you to a doctor directly. The nurse navigator at the help desk ({place_short(h, lang)}) passes your question to the doctor, "
                "and at the OPD you can ask the doctor directly."), []
    if label == "SCHEDULE":
        if state.get("pid") and store.next_tasks(state["pid"], actor=f"brain-{mode}", limit=1):
            return say_journey(state, lang, mode), []
        return ("इलाज कब शुरू होगा, यह डॉक्टर रिपोर्ट देखकर तय करते हैं। अगली ओपीडी में डॉक्टर से पूछिए; तारीख़ मिलते ही मैं आपको याद दिला दूँगी।" if hi else
                "When treatment starts is decided by the doctor after seeing the reports. Ask at your next OPD visit; once there's a date, I'll remind you."), []
    if label == "CONCESSION":
        return (TRAVEL["concession_hi"] if hi else TRAVEL["concession"]), []
    if label == "OUTLOOK":
        return ("मैं समझ सकती हूँ कि आप कितने परेशान हैं, यह सवाल हर परिवार के मन में आता है। इसका सही जवाब सिर्फ़ डॉक्टर ही दे सकते हैं, "
                "अगली बार ओपीडी में डॉक्टर से खुलकर पूछिए। अभी मन भारी हो तो काउंसलर या टेली-मानस 14416 से बात कर सकते हैं।" if hi else
                "I understand how worried you are; every family asks this. Only the doctors can answer it, so please ask them openly at the next OPD visit. "
                "If it feels heavy right now, the counsellors or Tele-MANAS 14416 are there to talk."), TELE_ACTION
    if label == "COST":
        u = PLACES[aspect]
        return (f"{_name(u, lang)} में ख़र्चे या मुफ़्त दवा की पक्की जानकारी मेरे पास नहीं है, वहीं पूछ लीजिए। "
                f"पैसों की दिक्कत हो तो सामाजिक कार्य और आर्थिक मदद डेस्क (विंग A, ग्राउंड फ़्लोर, काउंटर 12 से 13) मदद करता है।" if hi else
                f"I don't have confirmed cost details for {_name(u, lang)}; please ask there. If money is a problem, "
                f"the social work and financial aid desk (Wing A, ground floor, counters 12 to 13) can help."), []
    if label == "NAV_NEXT":
        return say_nav(PLACES[aspect], state, lang, text), []
    if label == "ASK_HOSPITAL":
        return ("माफ़ कीजिए। बताइए, आप किस अस्पताल में हैं या किस अस्पताल जा रहे हैं?" if hi else
                "Sorry. Which hospital are you at, or going to?"), []
    if label == "ACK":
        return ("ठीक है जी। आराम से जाइए, और कुछ चाहिए तो मुझसे पूछिए।" if hi else "Alright. Take your time, and ask me if you need anything else."), []
    if label == "FIRST_VISIT":
        reg = PLACES["A-G-REG"]
        return ((f"पहली बार आए हैं तो पहले नया रजिस्ट्रेशन कराइए: {place_short(reg, lang)}, {_f(reg, 'hours', lang)}। "
                 f"साथ लाइए: {_f(reg, 'bring', lang)}। वहीं UHID कार्ड बनेगा, फिर ओपीडी जाना होगा।") if hi else
                (f"For a first visit, register first: {place_short(reg, lang)}, {_f(reg, 'hours', lang)}. "
                 f"Bring: {_f(reg, 'bring', lang)}. You'll get your UHID card there, then go to the OPD.")), []
    if label == "TWO_PLACES":
        a, b = (PLACES[x] for x in aspect.split("|"))
        state["place"] = b["id"]
        return (f"{say_place(a, 'where', lang, 'voice')} और {say_place(b, 'where', lang, 'voice')}" if hi else
                f"{say_place(a, 'where', lang, 'voice')} And {say_place(b, 'where', lang, 'voice')}"), []
    if label == "AT_PLACE":
        return say_after(aspect, state, lang, mode), []
    if label == "NAME_SAID":
        nm = extract_name(text)
        store.update(state["pid"], f"brain-{mode}", name=nm)
        return (f"धन्यवाद {nm} जी। बताइए, क्या मदद करूँ?" if hi else f"Thank you, {nm}. How can I help?"), []
    if label in ("HOSP_TATA_IN", "HOSP_TATA_COMING", "HOSP_TATA"):
        state["hospital"] = "tata-parel"
        if state.get("pid"):
            store.update(state["pid"], f"brain-{mode}", hospital="tata-parel")
        if label == "HOSP_TATA_IN":
            state["where"] = "in"
            return ("जी, आप टाटा मेमोरियल हॉस्पिटल में हैं। बताइए, कहाँ जाना है या क्या जानना है?" if hi else
                    "Okay, you're at Tata Memorial Hospital. Where do you need to go, or what do you need?"), []
        if label == "HOSP_TATA_COMING":
            state["where"] = "coming"
            if state.get("pid"):
                store.update(state["pid"], f"brain-{mode}", stage="before_arrival")
            low_t = " " + text.lower() + " "
            nm, sc = public_match(low_t)
            if nm and sc >= 2 and re.search(QUESTION_RX, low_t):
                state["place"] = nm["id"]
                return say_place(nm, "where", lang, "voice"), []
            if re.search(TRAVEL_RX, low_t) or travel_state(low_t):
                return say_travel(text, lang, mode), []
            return ("जी, टाटा मेमोरियल हॉस्पिटल। मैं बता सकती हूँ कैसे पहुँचें, कौन से कागज़ात लाएँ, और पास में रहने की जगह। क्या जानना चाहेंगे?" if hi else
                    "Okay, Tata Memorial Hospital. I can tell you how to get there, which documents to bring, and where to stay nearby. What would you like?"), []
        return ("जी, टाटा मेमोरियल हॉस्पिटल। बताइए, क्या मदद करूँ?" if hi else "Okay, Tata Memorial Hospital. How can I help?"), []
    if label == "HOSP_OTHER":
        first = not state.get("told_other")
        state["hospital"], state["told_other"] = "other", True
        if state.get("pid"):
            store.update(state["pid"], f"brain-{mode}", hospital="other")
        if not first:                                  # already explained once: short and kind, not the same paragraph again
            return ("उस अस्पताल के अंदर का रास्ता मेरे पास नहीं है, माफ़ कीजिए। वहाँ के किसी स्टाफ़ या हेल्प डेस्क से पूछिए।" if hi else
                    "I don't have directions inside that hospital, sorry. Please ask any staff member or the help desk there."), []
        return ("माफ़ कीजिए, उस अस्पताल के अंदर का नक्शा मेरे पास नहीं है। अभी मैं सिर्फ़ टाटा मेमोरियल हॉस्पिटल, मुंबई के अंदर रास्ता बता सकती हूँ। "
                "वहाँ के हेल्प डेस्क से पूछिए। कागज़ात या मन की बात में मैं फिर भी मदद कर सकती हूँ।" if hi else
                "Sorry, I don't have the map inside that hospital. Right now I can only guide inside Tata Memorial Hospital, Mumbai. "
                "Please ask the help desk there. I can still help with documents or if you want to talk."), []
    if label == "META":
        state["place"] = None
        return ("माफ़ कीजिए, मुझसे ग़लती हुई। एक बार फिर बताइए, आपको कहाँ जाना है या क्या जानना है? जैसे 'ओपीडी कहाँ है' या 'पहली मंज़िल पर क्या क्या है'।" if hi else
                "Sorry, my mistake. Please tell me once more where you need to go or what you need, like 'where is the OPD' or 'what is on the first floor'."), []
    if label == "NEGATED":
        state["place"] = None
        return ("अच्छा, वहाँ नहीं। तो बताइए, कहाँ जाना है?" if hi else "Okay, not there. Where do you need to go?"), []
    if label == "FLOOR_LIST":
        return say_floor(aspect, lang, mode), []
    if label == "FOLLOWUP":
        label = state.get("place") or "NONE"
        if label == "NONE":
            return ("किस विभाग के बारे में पूछ रहे हैं? जैसे सीटी स्कैन, कीमो या ओपीडी।" if hi else
                    "Which department do you mean? For example CT scan, chemo or OPD."), []
    if label in ("C-2-PET", "C-1-CT") and aspect == "where" and re.search(PET_CT_RX, text.lower()):
        state["place"] = "C-2-PET"        # "पेट सीटी" can be PET-CT or a CT of the stomach: give both, from the JSON
        pet, ct = PLACES["C-2-PET"], PLACES["C-1-CT"]
        return (f"पेट-सीटी स्कैन {place_short(pet, lang)} पर है। अगर पेट का सादा सीटी स्कैन है, तो {place_short(ct, lang)}।" if hi else
                f"PET-CT is in {place_short(pet, lang)}. A regular CT scan is in {place_short(ct, lang)}."), []
    if label in PLACES:
        if state.get("place") != label:
            state["path_step"] = 0                   # a new destination: the walk starts again (step 0 was just said)
        state["place"] = label
        return say_place(PLACES[label], aspect, lang, mode), []
    if label == "SCAN_GENERIC":
        return ("कौन सा स्कैन? सीटी, एमआरआई, एक्स-रे, अल्ट्रासाउंड या पेट-सीटी?" if hi else
                "Which scan is it: CT, MRI, X-ray, ultrasound, or PET-CT?"), []
    if label == "OPD_GENERIC":
        state["place"] = "A-1-MED"                 # all OPDs are on Wing A, first floor: "pehli manzil?" can be confirmed
        return ("ओपीडी विंग A, पहली मंज़िल पर है, मुख्य लिफ़्ट से जाइए। आपका कमरा नंबर ओपीडी पर्ची पर छपा है।" if hi else
                "The OPDs are in Wing A, first floor; take the main lift. Your room number is printed on your OPD slip."), []
    if label == "GREETING":
        p = store.get(state["pid"], actor=f"brain-{mode}") if state.get("pid") else None
        nm = p and p.get("name")
        return ((f"नमस्ते {nm} जी! बताइए, आज क्या मदद करूँ?" if nm else "नमस्ते जी! बताइए, क्या मदद करूँ?") if hi else
                (f"Namaste {nm}! How can I help today?" if nm else "Namaste! How can I help?")), []
    if label == "BYE":
        return ("धन्यवाद जी। अपना ध्यान रखिए, ओन्कोसाथी हमेशा आपके साथ है।" if hi else "Thank you. Take care; OncoSaathi is always with you."), []
    low = " " + text.lower() + " "
    if re.search(HELP_RX, low) and len(text.split()) <= 9:
        state["misses"] = 0
        return ("जी ज़रूर। बताइए, कहाँ जाना है या क्या जानना है? जैसे रजिस्ट्रेशन, ओपीडी, कोई जाँच, रहने की जगह, या आज आपका अगला कदम।" if hi else
                "Of course. Tell me where you need to go or what you need: registration, OPD, a test, a place to stay, or your next step today."), []
    if state.get("place") in PLACES and re.search(CONFIRM_RX, low) and len(text.split()) <= 6 \
            and not re.search(r"\b(where|kahan|kahaan|kidhar)\b|कहाँ|कहां|किधर", low):
        state["misses"] = 0                       # "pehli manzil pe hai?" -> confirm (or correct) the last place
        u = PLACES[state["place"]]
        said = next((f for f, rx in FLOOR_WORDS.items() if re.search(rx, low)), None)
        wrong = said is not None and "floor" in u and said != u["floor"]
        lead = ("नहीं जी, ध्यान दीजिए: " if hi else "No, please note: ") if wrong else ("जी हाँ। " if hi else "Yes. ")
        return lead + say_place(u, "where", lang, "voice"), []
    state["misses"] = state.get("misses", 0) + 1
    if state["misses"] < 2:                       # first miss: ask, don't send them away
        return ("माफ़ कीजिए, मैं ठीक से समझ नहीं पाई। किस विभाग या काम के बारे में पूछ रहे हैं? जैसे ओपीडी, सीटी स्कैन, कीमो, रजिस्ट्रेशन। पर्ची हो तो उसकी फ़ोटो चैट में भेजिए।" if hi else
                "Sorry, I didn't quite get that. Which department or task do you mean? For example OPD, CT scan, chemo or registration. If you have a slip, send a photo of it in the chat."), []
    state["misses"] = 0
    return UNKNOWN[lang], []


# ---------------- opening flow: name -> hospital -> in hospital or coming ----------------
def extract_name(text):
    if re.search(r"\d", text) or len(text) > 60:
        return None
    m = re.search(r"(?:mera naam|my name is|naam|i am|i'm|मेरा नाम|नाम)\s+(.+)", text, re.I)
    cand = m.group(1) if m else text
    words = [w for w in re.split(r"[\s,.।!?]+", cand) if w and w.lower() not in NAME_STOP]
    if not 1 <= len(words) <= 3:
        return None
    if public_match(" " + text.lower() + " ")[1] >= 3 or re.search(STAY_RX + "|" + DOCS_RX + "|" + NEXT_STEP_RX, text.lower()):
        return None
    return " ".join(w[:1].upper() + w[1:] if w.isascii() else w for w in words[:2])


ASK_HOSPITAL = {"hi": "आप किस अस्पताल की बात कर रहे हैं?", "en": "Which hospital are you going to?"}
ASK_WHERE = {"hi": "आप अभी अस्पताल में हैं, या आने वाले हैं?", "en": "Are you at the hospital now, or planning to come?"}


GREETING = {
    "hi": "नमस्ते, मैं ओन्कोसाथी हूँ। मैं आपकी कैसे मदद कर सकती हूँ?",
    "en": "Hello, I'm OncoSaathi. How can I help you?",
}


def opening(state, lang="hi"):
    """First message of a chat or call: a short introduction only, no questions. Returning people are greeted by name.
    Where they are (hospital, in or coming) is worked out from what they say, never asked (see hospital_statement)."""
    state["lang"] = lang
    state["awaiting"] = None
    p = store.get(state["pid"], actor=f"brain-{state.get('mode')}") if state.get("pid") else None
    nm = p and p.get("name")
    if nm:
        return (f"नमस्ते {nm} जी, मैं ओन्कोसाथी हूँ। मैं आपकी कैसे मदद कर सकती हूँ?" if lang == "hi" else
                f"Hello {nm}, I'm OncoSaathi. How can I help you?")
    return GREETING["hi" if lang == "hi" else "en"]


def handle_awaiting(text, state, lang):
    """Answers to the opening questions. Returns a reply, or None to treat the text as a normal question."""
    hi = lang == "hi"
    t = " " + text.lower() + " "
    what = state.get("awaiting")
    if what == "name":
        nm = extract_name(text)
        if not nm:
            state["awaiting"] = None
            return None
        store.update(state["pid"], f"brain-{state.get('mode')}", name=nm)
        p = store.get(state["pid"], actor="brain")
        if p.get("hospital"):
            state["awaiting"] = None
            return f"धन्यवाद {nm} जी। बताइए, आज क्या मदद करूँ?" if hi else f"Thank you, {nm}. How can I help today?"
        state["awaiting"] = "hospital"
        return f"धन्यवाद {nm} जी। {ASK_HOSPITAL['hi']}" if hi else f"Thank you, {nm}. {ASK_HOSPITAL['en']}"
    if what == "hospital":
        if re.search(TATA_RX, t):
            store.update(state["pid"], f"brain-{state.get('mode')}", hospital="tata-parel")
            confirm = "ठीक है, टाटा मेमोरियल हॉस्पिटल, परेल। " if hi else "Okay, Tata Memorial Hospital, Parel. "
            if travel_state(t) or re.search(COMING_RX, t) or re.search(r"(जाना है|आना है|jaana hai|jana hai)", t):
                state["awaiting"], state["where"] = None, "coming"
                store.update(state["pid"], f"brain-{state.get('mode')}", stage="before_arrival")
                if re.search(QUESTION_RX, t):      # "Odisha se kaise aaun?" in the same breath: answer it now
                    return confirm + say_travel(text, lang, state.get("mode", "chat"))
                return confirm + ("आप बाहर से आ रहे हैं। मैं बता सकती हूँ: कैसे पहुँचें, कौन से कागज़ात लाएँ, और पास में रहने की जगह। क्या जानना चाहेंगे?" if hi else
                                  "You're coming from outside. I can tell you how to get here, which documents to bring, and where to stay nearby. What would you like?")
            if re.search(IN_RX, t):                 # "Tata Memorial, main andar hoon": both answered at once
                state["awaiting"], state["where"] = None, "in"
                return confirm + ("बताइए, आपको क्या परेशानी है, और आप अस्पताल में कहाँ जाना चाहते हैं?" if hi else
                                  "Tell me, what do you need, and where in the hospital do you want to go?")
            state["awaiting"] = "where"
            return (f"ठीक है, टाटा मेमोरियल हॉस्पिटल, परेल। {ASK_WHERE['hi']}" if hi else
                    f"Okay, Tata Memorial Hospital, Parel. {ASK_WHERE['en']}")
        if len(text.split()) <= 6 and not public_match(t)[0]:
            store.update(state["pid"], f"brain-{state.get('mode')}", hospital="other")
            state["awaiting"] = None
            return ("माफ़ कीजिए, अभी मेरे पास सिर्फ़ टाटा मेमोरियल, परेल का नक्शा है। उस अस्पताल के गेट पर हेल्प डेस्क से पूछिए। "
                    "कागज़ात या मन की बात में मैं फिर भी मदद कर सकती हूँ।" if hi else
                    "Sorry, right now I only have the map of Tata Memorial, Parel. Please ask the help desk at that hospital's gate. "
                    "I can still help with documents or if you want to talk.")
        state["awaiting"] = None
        return None
    if what == "where":
        state["awaiting"] = None
        if re.search(IN_RX, t):
            state["where"] = "in"
            return ("अच्छा, आप अस्पताल में हैं। बताइए, आपको क्या परेशानी है, और आप अस्पताल में कहाँ जाना चाहते हैं?" if hi else
                    "Okay, you're at the hospital. What do you need today: registration, OPD, a test, or your next step?")
        if re.search(COMING_RX, t):
            state["where"] = "coming"
            store.update(state["pid"], f"brain-{state.get('mode')}", stage="before_arrival")
            return ("ठीक है। आने से पहले मैं बता सकती हूँ: कैसे पहुँचें, कौन से कागज़ात लाने हैं, और पास में रहने की जगह। क्या जानना चाहेंगे?" if hi else
                    "Okay. Before you come, I can tell you how to get here, which documents to bring, and where to stay nearby. What would you like?")
        return None
    return None


def capture_facts(text, state):
    """Pick up the caller's name and hospital from anything they say (used on hosted-agent transcripts)."""
    t = " " + text.lower() + " "
    got = []
    if state.get("pid") and re.search(NAME_SAID_RX, t):
        nm = extract_name(text)
        if nm:
            store.update(state["pid"], "capture", name=nm)
            got.append("name")
    if re.search(TATA_RX, t):
        state["hospital"] = "tata-parel"
        if state.get("pid"):
            store.update(state["pid"], "capture", hospital="tata-parel")
        got.append("hospital")
    return got


def hospital_known(state):
    """'tata-parel', 'other', or None. Only what was said in THIS conversation counts: people move between hospitals,
    so every new chat or call asks again (the saved record is history, not an assumption)."""
    return state.get("hospital")


# "speak in Tamil", "tamil mein bolo", or just "Tamil". A language they say they don't understand is not a request.
LANG_WORDS = {"ta": r"tamil|தமிழ்", "kn": r"kannada|ಕನ್ನಡ", "hi": r"hindi|हिंदी|हिन्दी", "en": r"english|angrezi|अंग्रेज़ी|अंग्रेजी|इंग्लिश",
              "mr": r"marathi|मराठी", "te": r"telugu|తెలుగు", "ml": r"malayalam|മലയാളം", "bn": r"bengali|bangla|বাংলা",
              "gu": r"gujarati|ગુજરાતી", "pa": r"punjabi|ਪੰਜਾਬੀ", "od": r"odia|oriya|ଓଡ଼ିଆ"}
LANG_LABEL = {"ta": "Tamil", "kn": "Kannada", "hi": "Hindi", "en": "English", "mr": "Marathi", "te": "Telugu", "ml": "Malayalam",
              "bn": "Bengali", "gu": "Gujarati", "pa": "Punjabi", "od": "Odia"}
LANG_VERB_RX = (r"\b(speak|talk|reply|answer|respond|switch|change|language|understand|know|bhasha|bolo|boliye|bolna|batao|bataiye)\b|"
                r"भाषा|बोल|बताइए|बताओ|பேசு|மொழி|ಮಾತನಾಡ|ಭಾಷೆ|మాట్లాడ|భాష|സംസാരി|ഭാഷ|ভাষা|ভাষায়|ભાષા|ਭਾਸ਼ਾ|ଭାଷା|பதில்|ಉತ್ತರ")
NOT_LANG_RX = r"(don'?t|do not|cannot|can'?t|not|no)\s+(\w+\s+){0,3}?(WORD)|(WORD)\s+(\w+\s+){0,2}?(nahi|nahin|नहीं)"


def asked_language(text):
    """The language this message asks to be answered in, or None."""
    t = " " + text.lower().strip(" .!?।") + " "
    found = []
    for code, words in LANG_WORDS.items():
        cleaned = re.sub(NOT_LANG_RX.replace("WORD", words), " ", t)
        m = re.search(r"(?<![a-zऀ-ॿ])(" + words + r")(?![a-zऀ-ॿ])", cleaned)
        if m:
            found.append((m.start(), code))
    if not found:
        return None
    if len(t.split()) > 2 and not re.search(LANG_VERB_RX, t):
        return None                                      # "Tamil Nadu se aaya hoon" is not a language request
    return min(found)[1]


def language_request_has_question(text):
    """'Tamil mein batao, endoscopy kahan hai': change language and also answer it. Safety wording is always answered."""
    low = " " + text.lower() + " "
    if any(w in low for w in core.SELF_HARM + core.EMERGENCY + core.CLINICAL + core.EMOTIONAL):
        return True
    return public_match(low)[1] >= 3


async def here_reply(state):
    """The chat's "I'm here" picker, in the language of the conversation."""
    lang = state.get("lang") or "hi"
    out = i_am_at(state, "hi" if lang == "hi" else "en")
    return await in_language(out, lang)


def i_am_at(state, lang):
    """They told us where they are standing: redraw the route they were following, or ask where they want to go."""
    hi = lang == "hi"
    here, dest = state["here"], state.get("walk_to")
    where = place_name(here, lang)
    if not dest:
        text = (f"ठीक है, आप {where} पर हैं। बताइए, कहाँ जाना है?" if hi else f"Okay, you're at {where}. Where would you like to go?")
        return {"text": text, "lang": lang, "route": "I_AM_AT", "actions": []}
    if here == dest:
        text = (f"बढ़िया, आप {where} पहुँच गए। आगे कुछ पूछना हो तो बताइए।" if hi else f"Good, you've reached {where}. Tell me if you need anything else.")
        return {"text": text, "lang": lang, "route": "I_AM_AT (arrived)", "actions": []}
    return {"text": walk_text(here, dest, lang), "lang": lang, "route": "I_AM_AT (new route)", "actions": [],
            "map": map_for(here, dest, lang, True)}


async def translate(text, src, tgt):
    """sarvam-translate:v1. Returns None on failure (the caller then answers in Hindi)."""
    try:
        r = await _http().post(f"{SARVAM}/translate", headers={"api-subscription-key": core.SARVAM_KEY}, timeout=8,
                               json={"input": text[:1900], "source_language_code": src, "target_language_code": tgt,
                                     "model": "sarvam-translate:v1"})
        r.raise_for_status()
        return r.json().get("translated_text")
    except Exception as e:
        print(f"[brain] translate {src}->{tgt} failed: {e!r}", flush=True)
        return None


async def respond(text, state, stt_lang=None):
    """One turn. Returns {"text", "lang", "route", "actions"}.
    Hindi and English are answered directly. Other Indian languages (Odia, Marathi, Bengali, Tamil…) are translated to
    English, answered from the same data and templates, and the answer is translated back, so the facts never change."""
    lang = core.pick_lang(text, "Auto", stt_lang)
    native = bool(re.search(r"[^\x00-\x7f]", text))
    asked = asked_language(text)
    if asked:
        state["lang_pref"] = asked
    elif native and state.get("lang_pref") and lang != state["lang_pref"]:
        state.pop("lang_pref")           # they've started writing another language: follow them
    if state.get("lang_pref"):
        lang = state["lang_pref"]        # "speak in Tamil" holds even when they type in English letters
    elif lang == "en" and state.get("lang") not in (None, "en") and len(text.split()) <= 4:
        lang = state["lang"]             # "Tata Memorial", "Radiology": names alone don't switch the conversation's language
    state["lang"] = lang
    if asked and not language_request_has_question(text):
        out = {"text": f"Okay, I'll reply in {LANG_LABEL[asked]}. Where would you like to go?", "lang": "en",
               "route": f"LANGUAGE/{asked}", "actions": []}
        if asked == "hi":
            out["text"], out["lang"] = "ठीक है, मैं हिंदी में जवाब दूँगी। बताइए, कहाँ जाना है?", "hi"
        return await in_language(out, asked)
    if lang in ("hi", "en"):
        return await _respond(text, state, lang)
    src = core.LANG_CODE[lang]
    en = (await translate(text, src, "en-IN")) if native else text   # English letters go straight to the rules
    if not en:
        return await _respond(text, state, "hi")
    out = await _respond(en, state, "en", original=text)
    return await in_language(out, lang)


async def in_language(out, lang):
    """Translate a finished English answer. Facts stay the same; the department keeps its English name, which is what the
    map shows, because machine translation can get a medical name wrong."""
    if lang in ("hi", "en") or out.get("lang") == lang:
        return out
    src = core.LANG_CODE[lang]
    name = PLACES[out["map"]["to"]]["name"] if out.get("map") else ""
    named = bool(name) and out["text"].startswith(name)
    back = await translate(out["text"][len(name):].lstrip(": ") if named else out["text"], "en-IN", src)
    if back:
        out["text"], out["lang"] = (f"{name}: {back}" if named else back), lang
    if back and out.get("map"):
        out["map"]["caption"] = await translate(out["map"]["caption"], "en-IN", src) or out["map"]["caption"]
    out["route"] += f" [{lang} via translate]"
    return out


async def _respond(text, state, lang, original=None):
    t0 = time.perf_counter()
    mode = state.get("mode", "chat")
    state["text"] = text
    low = " " + text.lower() + " "
    safety_hit = any(w in low for w in core.SELF_HARM + core.EMERGENCY)
    if state.get("awaiting") and state.get("pid") and not safety_hit:
        reply = handle_awaiting(text, state, lang)
        if reply:
            return {"text": reply, "lang": lang, "route": f"OPENING/{state.get('awaiting') or 'done'}", "actions": []}
    if re.search(WHO_RX, low) and not safety_hit:            # "आप कौन हो?": introduce OncoSaathi again
        return {"text": GREETING["hi" if lang == "hi" else "en"], "lang": lang, "route": "INTRO via rules-who", "actions": []}
    # "मैं फ़ार्मेसी पर हूँ" on its own: remember it, and redraw the route they were following from there
    unsafe = safety_hit or any(w in low for w in core.CLINICAL + core.EMOTIONAL)
    spot = None if unsafe or state.get("hospital") != "tata-parel" else spoken_start(text, state.get("walk_to"))
    if spot and spot[1] == "at" and not re.search(QUESTION_RX + "|" + WHERE_NEXT_RX, low) and not two_places(low):
        state["here"], state["here_told"] = spot[0], True
        return i_am_at(state, lang)
    decision, source = await decide(text, state)
    label = decision["label"]
    if spot:
        state["here"], state["here_told"] = spot[0], True
        others = [p for p in (two_places(low) or []) if p != spot[0]]
        if label == spot[0] and others:                # "pharmacy se radiology kaise jaun": they want the other one
            decision, source = {"label": others[0], "aspect": "where"}, "rules-from-to"
    elif state.get("walk_to") and re.search(_ARRIVED_RX, low) and not re.search(LIFT_RX + "|" + LOBBY_RX, low) \
            and not any(re.search(rx, low) for rx in FLOOR_WORDS.values()):
        state["here"], state["here_told"] = state["walk_to"], True   # they reached the room we guided them to
    if decision["label"] != "NONE":
        state["misses"] = 0
    label = decision["label"]
    hosp = hospital_known(state)
    held = state.pop("held_question", None)
    looks_like_hospital = (re.search(OTHER_NAMES_RX, low) or re.search(r"\b(hospital|medical|institute|clinic)\b|अस्पताल|हॉस्पिटल", low)) \
        and not re.search(QUESTION_RX + "|" + DEICTIC, low) and len(text.split()) <= 6
    if held and not label.startswith("HOSP_") and label not in SAFETY and label not in PLACES and looks_like_hospital:
        decision, source, label = {"label": "HOSP_OTHER", "aspect": "where"}, "rules-held-other", "HOSP_OTHER"
    elif held and not label.startswith("HOSP_"):
        state["held_question"] = held            # they asked something else instead: keep the first question waiting
    if held and label.startswith("HOSP_TATA"):
        # they named the hospital we asked for: confirm, then answer the question they asked first
        say(decision, state, lang, mode, text)
        out = await _respond(held, state, lang)
        lead = "जी, टाटा मेमोरियल हॉस्पिटल। " if lang == "hi" else "Okay, Tata Memorial Hospital. "
        out["text"] = lead + out["text"]
        out["route"] = f"HELD_QUESTION via rules-hospital -> {out['route']}"
        return out
    if (label in PLACES or label in NEEDS_HOSPITAL or label in ("TWO_PLACES", "STAY_PLUS")) and hosp is None:
        state["held_question"] = text          # hospital-specific question before we know the hospital: ask first
        return {"text": ("ज़रूर, मैं मदद करूँगी। पहले बताइए, आप किस अस्पताल की बात कर रहे हैं?" if lang == "hi" else
                         "Sure, I'll help. First, which hospital are you at, or going to?"),
                "lang": lang, "route": f"ASK_HOSPITAL (held {label})", "actions": []}
    if (label in PLACES or label in (NEEDS_HOSPITAL - {"TRAVEL"}) or label in ("TWO_PLACES", "STAY_PLUS")) and hosp == "other":
        decision, source = {"label": "HOSP_OTHER", "aspect": "where"}, "rules-other-hospital"
    reply, actions = say(decision, state, lang, mode, text)
    if re.search(META_RX, low) and decision["label"] in PLACES:
        reply = ("माफ़ कीजिए। " if lang == "hi" else "Sorry about that. ") + reply
    said_floor = next((f for f, rx in FLOOR_WORDS.items() if re.search(rx, low)), None)
    if decision["label"] in PLACES and said_floor and "floor" in PLACES[decision["label"]] and said_floor != PLACES[decision["label"]]["floor"] \
            and decision["aspect"] == "where":
        reply = ("ध्यान दीजिए, यह उस मंज़िल पर नहीं है। " if lang == "hi" else "Note: it's not on that floor. ") + reply
    if reply == state.get("last_reply") and decision["label"] in ("TRAVEL", "DOCUMENTS", "STAY") and state.get("last_reply_lang") == state.get("lang"):
        if decision["label"] == "DOCUMENTS":
            reg = PLACES["A-G-REG"]
            reply = (f"ये कागज़ात रजिस्ट्रेशन पर दिखाने होते हैं: {place_short(reg, lang)}, {_f(reg, 'hours', lang)}। वहीं UHID कार्ड बनता है। "
                     f"बाक़ी बातों के लिए अस्पताल का नंबर {TRAVEL['phone']} है।" if lang == "hi" else
                     f"Show these documents at registration: {place_short(reg, lang)}, {_f(reg, 'hours', lang)}. You get your UHID card there. "
                     f"For anything else, the hospital's number is {TRAVEL['phone']}.")
        else:
            reply = (f"इस बारे में इससे ज़्यादा पक्की जानकारी मेरे पास नहीं है। अस्पताल के नंबर {TRAVEL['phone']} पर पूछ लीजिए।" if lang == "hi" else
                     f"I don't have more confirmed detail on that. Please check with the hospital on {TRAVEL['phone']}.")
    rep_place = decision["label"] if decision["label"] in PLACES else (state.get("place") if decision["label"] == "FOLLOWUP" else None)
    same_lang = state.get("last_reply_lang") == state.get("lang")
    if reply == state.get("last_reply") and rep_place in PLACES and same_lang:
        # the same words again help nobody: give the next useful fact about that place instead
        u = PLACES[rep_place]
        for alt in ("hours", "note", "bring", "way"):
            if u.get(alt if alt != "way" else "path"):
                cand = say_place(u, alt, lang, "voice")
                if cand != reply:
                    reply = cand
                    break
    walk = None
    dest = walk_dest(decision, state)
    if dest:
        start, told = state.get("here") or "GATE1", bool(state.get("here_told"))
        if start != dest:
            state["walk_to"] = dest
            walk = map_for(start, dest, lang, told)
            if start != "GATE1" and decision["label"] == dest:     # the stored path starts at Gate 1; this one must not
                reply = walk_text(start, dest, lang)
    state["last_reply"], state["last_label"], state["last_reply_lang"] = reply, decision["label"], state.get("lang")
    if state.get("pid"):
        store.audit(f"brain-{mode}", state["pid"], "turn", f"{decision['label']}/{decision['aspect']} via {source}")
    ms = int((time.perf_counter() - t0) * 1000)
    out = {"text": reply, "lang": lang, "route": f"{decision['label']}/{decision['aspect']} via {source} ({ms} ms)", "actions": actions}
    if walk:
        out["map"] = walk
    return out
