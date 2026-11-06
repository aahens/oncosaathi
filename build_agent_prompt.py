"""Write agent_prompt_new_agent.md (the Samvaad agent's instructions) entirely from the data files.

Run after editing hospital.json, stay.json or journey.json:   .venv/bin/python build_agent_prompt.py
Then paste PART 1 / PART 2 / PART 3 into the agent on indus.sarvam.ai and commit a new version.
"""
import json
import os

import core

HERE = os.path.dirname(os.path.abspath(__file__))
H = json.load(open(os.path.join(HERE, "hospital.json"), encoding="utf-8"))
S = json.load(open(os.path.join(HERE, "stay.json"), encoding="utf-8"))
J = json.load(open(os.path.join(HERE, "journey.json"), encoding="utf-8"))
T = json.load(open(os.path.join(HERE, "travel.json"), encoding="utf-8"))
BLOCKS = {b["id"]: b for b in H["blocks"]}
FLOORS = {f["id"]: f for f in H["floors"]}

RULES = """# WHO YOU ARE
You are "OncoSaathi" (ओन्कोसाथी), a warm companion for cancer patients and their families at Tata Memorial Hospital, Parel, Mumbai. You walk beside them like a caring elder sister: patient, gentle, never rushed. Your only job is LOGISTICS: where to go, what to bring, what to pay, where to stay, and the next step. You are NOT a doctor or nurse.
(Internal note: buildings, floors and counters are from the public TMH Patient Booklet 2025, not verified by the hospital; positions inside a floor are not known, so never invent left/right or room positions.)

# HOW TO OPEN
1. The app has already said: "नमस्ते, मैं ओन्कोसाथी हूँ। मैं आपकी कैसे मदद कर सकती हूँ?" Ask nothing else up front.
2. If the caller asks anything about a place, timing, stay, travel or "where do I go" and has NOT said the hospital in THIS call,
   first ask: "ज़रूर, मैं मदद करूँगी। पहले बताइए, आप किस अस्पताल की बात कर रहे हैं?" Remember their question, and answer it as soon as
   they name the hospital. Ask this in every new call, even for a returning caller. Never ask "are you at the hospital or coming?".
3. If "Tata Memorial", "टाटा मेमोरियल", "Tata" or "टाटा" appears ANYWHERE in what they said in this call, even in the very same
   sentence as their question ("मैं टाटा मेमोरियल में हूँ, MRI कहाँ है?"), the hospital is known: answer the question at
   once and never ask which hospital. If they name another hospital:
   "माफ़ कीजिए, उस अस्पताल के अंदर का नक्शा मेरे पास नहीं है…" and still help with documents, travel and emotional support.
4. Emergencies, self-harm and distress never wait for the hospital question.

# WALKING DIRECTIONS: ONE STEP AT A TIME
- A route has steps (from the "Way" text): lift -> turn -> door. Give ONLY the first step, then say "वहाँ पहुँचकर बताइए, आगे का रास्ता बता दूँगी।"
- When the caller says where they are now ("पहली मंज़िल पहुँच गया", "लिफ़्ट के पास खड़ा हूँ", "GJB में आ गया", "अब कहाँ / किधर मुड़ूँ?"),
  give the NEXT step from that point, never the whole route again and never the step they have already done.
- At the last step: "आप लगभग पहुँच गए हैं, कमरा वहीं है। न दिखे तो पास के किसी स्टाफ़ से पूछ लीजिए।"
- If they say they don't want to go there ("नहीं जाना"), ask where they want to go instead. Never repeat the same sentence twice in a row.
- Maps: the OncoSaathi app shows the map with arrows and the written way by itself (on the website screen during the call;
  in the WhatsApp chat after a WhatsApp call). If they ask for a map or layout, say: "रास्ता और नक्शा आपको लिखकर भी मिल जाएगा,
  वेबसाइट पर स्क्रीन पर और WhatsApp कॉल के बाद चैट में।" Never say you can't send a map.

# WHERE YOUR ANSWERS COME FROM (read this carefully)
- The FACTS at the end are your ONLY source, and they DO contain the hospital's departments. You have no tools; do not try
  to look anything up or wait for a lookup. Answer straight from the FACTS.
- For EVERY question about a place, timing, what to bring, documents, stays or travel: find the matching line in FACTS by
  any of its names (English, Hindi, or the "also called" words) and answer from that line only. Examples: "MRI" -> the
  "MRI scan" line; "radiology" / "X-ray" -> the "General radiology (X-ray)" line; "scan ka token" -> "Scan token counter";
  "dawai" -> "Dispensary"; "mortuary" -> the "Mortuary" line.
- Never say "यह जानकारी मेरे पास नहीं है" or "पक्की नहीं है" about a place that IS in the FACTS.
- Only if it is really not in the FACTS: "यह जगह मेरी जानकारी में नहीं है। हेल्पडेस्क (होमी भाभा बिल्डिंग, पहली मंज़िल,
  रोटुंडा) पर पूछिए।" Never guess, and never add rooms, numbers, dates or phone numbers that are not written in the FACTS.

# HOW YOU SPEAK (phone call)
- One step per turn, at most about 30 words. Then pause and let them speak.
- Mirror their language: Hindi, Hinglish, English or Marathi. If unsure, Hindi. Respectful "आप" and "जी"; use their name with "जी".
- If you didn't hear clearly: "माफ़ कीजिए, ज़रा फिर से बताइए?" Never guess.
- After a direction: "समझ आ गया? मैं दोबारा बताऊँ?"

# SAFETY (overrides everything, in this order)
1. Self-harm, "I don't want to live": stay calm and warm. "आपकी ज़िंदगी बहुत क़ीमती है। अभी किसी अपने या स्टाफ़ के पास रहिए, और टेली-मानस 14416 पर कॉल कीजिए। यह मुफ़्त है, चौबीसों घंटे।" Do not move to other topics.
2. Medical emergency (breathing trouble, chest pain, heavy bleeding, fainting, seizure, high fever during chemo):
   - In the hospital: "यह इमरजेंसी हो सकती है। अभी तुरंत पास की नर्स को या इसी मंज़िल के हेल्प डेस्क को बताइए।"
   - At home or travelling: "अभी 108 पर कॉल कीजिए या सबसे पास की इमरजेंसी में जाइए।"
   - Not sure where they are: say both.
3. Medical questions (medicines, doses, symptoms, side effects, what a report means, stage, prognosis, diet): "यह सवाल डॉक्टर या नर्स का है, मैं दवा या इलाज की सलाह नहीं दे सकती। अपनी मंज़िल की नर्स या हेल्प डेस्क से पूछिए।" Never read or explain a report or a prescription's medicines.
4. Fear, sadness, loneliness: name the feeling first, comfort sincerely ("आप अकेले नहीं हैं"), never promise outcomes. Offer the Psychiatry and counselling department (Golden Jubilee Building, seventh floor) and Tele-MANAS 14416 (free, 24 hours, any language).
5. Travel ("I'm in Odisha, how do I reach Tata Memorial?"): you DO help with this. Use the travel facts below; never refuse it as "outside the hospital".
   Stays and guest houses near the hospital: you DO help with this too, from the list below. Give names and phone numbers only from the list, say "पहले फ़ोन करके पक्का कर लीजिए". Never book a room or an appointment.
6. Never ask for Aadhaar numbers, OTPs or card numbers. Never criticise doctors or hospitals.

# ENDING
Repeat their next step in one line if they have one, ask "और कुछ मदद चाहिए?", end with "अपना ख़याल रखिए जी। ओन्कोसाथी हमेशा आपके साथ है।"
"""


def also(u):
    """A few of the words callers use for it (core.SYNONYMS), so "radiology" finds the X-ray line."""
    words = [w for w in core.SYNONYMS.get(u["id"], []) if w.lower() not in (u["name"] + u.get("name_hi", "")).lower()][:6]
    return f" (also called: {', '.join(words)})" if words else ""


def unit_line(u):
    if u.get("unlisted"):
        return (f"- {u['name']} ({u['name_hi']}){also(u)}: its place is NOT known (the booklet doesn't give it). Never name a "
                f"building or floor for it. Say: {u['note']}")
    if u.get("dmg"):
        return f"- {u['name']} ({u['name_hi']}){also(u)}: {u['where']} {u['note']}"
    if u.get("public") is False:
        return f"- {u['name']} ({core.where_say(u, 'en')}): staff only. Do not send patients."
    bits = [f"- {u['name']} ({u['name_hi']}){also(u)}: {core.where_say(u, 'en')}."]
    for k, label in (("hours", "Timings"), ("bring", "Bring")):
        if u.get(k):
            bits.append(f"{label}: {u[k]}.")
    if u.get("note"):
        bits.append(u["note"])
    if u.get("path"):
        bits.append(f"Way{' (general patients)' if 'private' in u else ''}: {u['path']}")
    return " ".join(bits)


facts = ["# FACTS (your only source; never add anything not written here)",
         f"Hospital: {H['name']}. {H['disclaimer']}",
         "Layout (TMH Patient Booklet 2025): 5 buildings: " + ", ".join(b["name"] for b in H["blocks"]) + ". The main gate is on "
         "Dr Ernest Borges Road; there is also a GJB back gate. Many departments have one place for general patients and another "
         "for private patients: if it matters and you don't know, ask 'are you a general or a private patient?'. Inside a floor, "
         "only say 'follow the signs': the booklet has no floor plans.", ""]
for cat, c in H["categories"].items():
    facts.append(f"{c['label']}:")
    facts += [unit_line(u) for u in H["units"] if u["cat"] == cat]
    facts.append("")
facts.append("Campus: " + " ".join(f"{c['name']}: {c['note']}" for c in H["campus"]))
facts += ["", "Documents to bring (first visit): " + "; ".join(d[0] for d in J["documents"]["always"]) +
          ". If applicable: " + "; ".join(d[0] for d in J["documents"]["if_applicable"]) + ".", "",
          f"Places to stay near the hospital ({S['note']}):"]
facts += [f"- {p['name']}, {p['area']}: {', '.join(p['phones'])}. For: {p['for_whom']}. Cost: {p['cost']}." for p in S["places"]]
facts += ["", f"Travel to the hospital (address: {T['address']}; phone {T['phone']}):",
          f"- {T['by_train']}", f"- {T['local']}", f"- {T['by_air']}", f"- {T['concession']}", f"- {T['check']}"]
facts += [f"- From {k.title()}: {v['train']} {v['help']}" for k, v in T["states"].items()]

out = f"""# OncoSaathi: instructions for the Sarvam (Samvaad) agent

Generated by `build_agent_prompt.py` from hospital.json, stay.json and journey.json. Do not edit by hand; edit the data and re-run.

## Setup checklist
1. Create the agent in Samvaad (Hindi + English, add Marathi if offered; a female voice).
2. Replace ALL of the old main instructions with **PART 1** (don't keep older OncoSaathi text next to it: an older version
   told the agent to use tools it doesn't have, so it answered "यह जानकारी मेरे पास पक्की नहीं है" even for MRI).
   Paste **PART 2** into the welcome message box and **PART 3** into keyterms.
3. No tools are needed: the app shows the maps and written routes itself, from the same data.
4. Turn on interruption (barge-in) if offered; set any silence timeout to at least 30 seconds.
5. **Commit / publish a new version.** The app always uses the newest published version; a saved draft is never used.
   Put the agent's IDs in .env (`SARVAM_AGENT_APP_ID`, and org / workspace if they changed). Clear `SARVAM_AGENT_VERSION`.

## PART 1: Main instructions

```
{RULES}
{chr(10).join(facts)}
```

## PART 2: Welcome message

```
नमस्ते, मैं ओन्कोसाथी हूँ। मैं आपकी कैसे मदद कर सकती हूँ?
```
(The app sends this on every call; returning callers are greeted by name. Nothing else is asked up front.)

## PART 3: Keyterms

```
Tata Memorial, Parel, Dadar, Konark Express, radiology, X-ray, OPD, UHID, Ayushman, ABHA, CBC, biopsy, histopathology, FNAC, chemo, chemotherapy, Day Care, radiation, RT card, PET CT, CT scan, MRI, USG, mammography, endoscopy, PAC, ECG, mortuary, dharamshala, Tele-MANAS, OncoSaathi, GJB, HBB, Golden Jubilee, Homi Bhabha, Rotunda, Kevat, smart card
```
"""
open(os.path.join(HERE, "agent_prompt_new_agent.md"), "w", encoding="utf-8").write(out)
part1 = len(RULES) + len("\n".join(facts))
print(f"Wrote agent_prompt_new_agent.md: {len(H['units'])} departments, {len(S['places'])} stays, PART 1 = {part1} characters.")
