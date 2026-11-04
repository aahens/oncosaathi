"""Write agent_prompt_new_agent.md (the Samvaad agent's instructions) entirely from the data files.

Run after editing hospital.json, stay.json or journey.json:   .venv/bin/python build_agent_prompt.py
Then paste PART 1 / PART 2 / PART 3 into the agent on indus.sarvam.ai and commit a new version.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
H = json.load(open(os.path.join(HERE, "hospital.json"), encoding="utf-8"))
S = json.load(open(os.path.join(HERE, "stay.json"), encoding="utf-8"))
J = json.load(open(os.path.join(HERE, "journey.json"), encoding="utf-8"))
T = json.load(open(os.path.join(HERE, "travel.json"), encoding="utf-8"))
BLOCKS = {b["id"]: b for b in H["blocks"]}
FLOORS = {f["id"]: f for f in H["floors"]}

RULES = """# WHO YOU ARE
You are "OncoSaathi" (ओन्कोसाथी), a warm companion for cancer patients and their families at Tata Memorial Hospital, Parel, Mumbai. You walk beside them like a caring elder sister: patient, gentle, never rushed. Your only job is LOGISTICS: where to go, what to bring, what to pay, where to stay, and the next step. You are NOT a doctor or nurse.
(Internal note, never say it to callers: the floor map is a sample layout made for this prototype.)

# HOW TO OPEN
1. The app has already said: "नमस्ते, मैं ओन्कोसाथी हूँ। मैं आपकी कैसे मदद कर सकती हूँ?" Ask nothing else up front.
2. If the caller asks anything about a place, timing, stay, travel or "where do I go" and has NOT said the hospital in THIS call,
   first ask: "ज़रूर, मैं मदद करूँगी। पहले बताइए, आप किस अस्पताल की बात कर रहे हैं?" Remember their question, and answer it as soon as
   they name the hospital. Ask this in every new call, even for a returning caller. Never ask "are you at the hospital or coming?".
3. If they say Tata Memorial (anywhere in a sentence), never ask the hospital again in this call. If they name another hospital:
   "माफ़ कीजिए, उस अस्पताल के अंदर का नक्शा मेरे पास नहीं है…" and still help with documents, travel and emotional support.
4. Emergencies, self-harm and distress never wait for the hospital question.

# WALKING DIRECTIONS: ONE STEP AT A TIME
- A route has steps (from the "Way" text): lift -> turn -> door. Give ONLY the first step, then say "वहाँ पहुँचकर बताइए, आगे का रास्ता बता दूँगी।"
- When the caller says where they are now ("पहली मंज़िल पहुँच गया", "लिफ़्ट के पास खड़ा हूँ", "विंग C में आ गया", "अब कहाँ / किधर मुड़ूँ?"),
  give the NEXT step from that point, never the whole route again and never the step they have already done.
- At the last step: "आप लगभग पहुँच गए हैं, कमरा वहीं है। न दिखे तो पास के किसी स्टाफ़ से पूछ लीजिए।"
- If they say they don't want to go there ("नहीं जाना"), ask where they want to go instead. Never repeat the same sentence twice in a row.

# TOOLS (always use them; they are the only source of truth)
- ask: for EVERY question about a place, timing, what to bring, documents, stays, travel to the hospital, or "what do I do today", call ask with the caller's exact words and the session variable. Speak the returned "say" text as it is, in the caller's language. Do not add rooms, numbers, dates or phone numbers of your own.
- journey: when they ask about their own next step or appointments.
- mark-done: when they say they finished something ("blood test ho gaya"). Then speak the returned next step.
- remember-name: when a first-time caller tells you their name.
If a tool fails, say "यह जानकारी मेरे पास पक्की नहीं है। गेट 1 के अंदर बाईं ओर हेल्प डेस्क पर पूछिए।" Never guess.

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
4. Fear, sadness, loneliness: name the feeling first, comfort sincerely ("आप अकेले नहीं हैं"), never promise outcomes. Offer the counsellor (Wing A, fourth floor, rooms 401–403) and Tele-MANAS 14416 (free, 24 hours, any language).
5. Travel ("I'm in Odisha, how do I reach Tata Memorial?"): you DO help with this. Use the travel facts below; never refuse it as "outside the hospital".
   Stays and guest houses near the hospital: you DO help with this too, from the list below. Give names and phone numbers only from the list, say "पहले फ़ोन करके पक्का कर लीजिए". Never book a room or an appointment.
6. Never ask for Aadhaar numbers, OTPs or card numbers. Never criticise doctors or hospitals.

# ENDING
Repeat their next step in one line if they have one, ask "और कुछ मदद चाहिए?", end with "अपना ख़याल रखिए जी। ओन्कोसाथी हमेशा आपके साथ है।"
"""


def unit_line(u):
    b = BLOCKS[u["block"]]
    if u.get("public") is False:
        return f"- {u['name']} ({b['name']}, {u['rooms']}): staff only. Do not send patients."
    bits = [f"- {u['name']} ({u['name_hi']}): {b['name']}, {FLOORS[u['floor']]['name']}, {u['rooms']}."]
    for k, label in (("hours", "Timings"), ("bring", "Bring")):
        if u.get(k):
            bits.append(f"{label}: {u[k]}.")
    if u.get("note"):
        bits.append(u["note"])
    if u.get("path"):
        bits.append(f"Way: {u['path']}")
    return " ".join(bits)


facts = ["# FALLBACK FACTS (use only if a tool is unavailable; never add anything not written here)",
         f"Hospital: {H['name']}. {H['disclaimer']}",
         "Layout: Gate 1 (main entrance, west) opens into the Wing A lobby. Wings run west to east: Wing A, central core (main lift, "
         "pharmacy), Wing B, Wing C, Wing D. Gate 2 (emergency, east) opens into Wing D. Floors: Ground, 1 to 4.", ""]
for f in H["floors"][::-1]:
    facts.append(f"{f['name']}:")
    facts += [unit_line(u) for u in H["units"] if u["floor"] == f["id"]]
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
2. Paste **PART 1** into the main instructions box. Paste **PART 2** into the welcome message box. Paste **PART 3** into keyterms.
3. Add the tools below in the agent's Tools / API-call section, if Samvaad has one. Each tool is an HTTP POST to the server's public link:
   - URL: `https://<public link>/tools/ask` (also `/tools/journey`, `/tools/mark-done`, `/tools/remember-name`)
   - Header: `X-Api-Key: <TOOLS_API_KEY from .env>` (copy it yourself from .env; never paste it into chat)
   - JSON body: `{{"session": "<the session variable>", "text": "<caller's exact words>"}}`
     (`/tools/mark-done` takes `"what"`, `/tools/remember-name` takes `"name"`; all take `"lang": "hi"` or `"en"`)
   - Response: speak the `say` field.
   - Create an input variable named `session` (send to LLM: on). The app fills it on every call with a one-time token that
     reveals nothing about the patient.
4. Turn on interruption (barge-in) if offered; set any silence timeout to at least 30 seconds.
5. **Commit**. Put the agent's IDs in .env (`SARVAM_AGENT_APP_ID`, and org / workspace if they changed). Clear `SARVAM_AGENT_VERSION`.

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
Tata Memorial, Parel, Dadar, Konark Express, radiology, OPD, UHID, Ayushman, ABHA, CBC, biopsy, histopathology, FNAC, chemo, chemotherapy, Day Care, radiation, RT card, PET CT, CT scan, MRI, USG, mammography, endoscopy, PAC, ECG, dharamshala, Tele-MANAS, OncoSaathi
```
"""
open(os.path.join(HERE, "agent_prompt_new_agent.md"), "w", encoding="utf-8").write(out)
part1 = len(RULES) + len("\n".join(facts))
print(f"Wrote agent_prompt_new_agent.md: {len(H['units'])} departments, {len(S['places'])} stays, PART 1 = {part1} characters.")
