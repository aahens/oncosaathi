"""Two-agent test: a simulated patient (sarvam-105b role-playing a real person) talks to the OncoSaathi assistant.

Each persona starts the conversation after OncoSaathi's greeting, reacts to the answers like a real person would
(follow-ups, confusion, giving the hospital name when asked), and at the end says whether its need was met.
Automatic flags: rooms not in our data, help-desk dead ends, repeated replies, over-long spoken replies.

Run:  .venv/bin/python simulate_patients.py            all 20 personas
      .venv/bin/python simulate_patients.py 1 5 9      only those personas
Writes a readable transcript to sim_results/ (temporary database; real patient data is never touched).
"""
import asyncio
import datetime as dt
import json
import os
import re
import sys
import tempfile

os.environ["ONCO_DB"] = os.path.join(tempfile.mkdtemp(), "sim.db")
import httpx

import brain
import core
import patient_store as store

MAX_TURNS = 6
PERSONAS = [
    ("At registration, no hospital named", "You are a 45-year-old man from Bihar, speaking Hinglish. You are standing at the registration desk and don't know where to go next. Do NOT say the hospital name unless asked; if asked, you are at Tata Memorial."),
    ("Caregiver needs cheap stay", "You are Sunita, 38, a caregiver from a village in UP, speaking simple Hindi. Your husband will be treated for weeks. You need a cheap or free place to stay near the hospital. You are going to Tata Memorial; say so only if asked."),
    ("Elderly, blood test", "You are a 70-year-old grandmother speaking slow, simple Hindi. The doctor told you to get a blood test. You are already inside Tata Memorial and want to know where to go."),
    ("Coming from Odisha", "You are Bikash, 30, from Cuttack, Odisha, speaking Hindi mixed with a few Odia words. Your father has an appointment at Tata Memorial next week. You want to know how to travel there and what documents to bring."),
    ("Marathi speaker, radiology", "You are a 55-year-old Marathi-speaking man. Speak only in Marathi (Devanagari). The doctor wrote 'Radiology' on your slip. You are at Tata Memorial."),
    ("Chemo patient with high fever", "You are a 35-year-old woman on chemotherapy, at home, speaking Hindi. Since last night you have a high fever (102) and shivering. You are worried and want to know what to do."),
    ("Scared son", "You are a 24-year-old son whose mother was just diagnosed with cancer. You are scared, crying, and feel alone. Speak Hinglish. You need emotional support more than directions."),
    ("Asks medicine dose", "You are a 50-year-old man speaking Hinglish. You want to know how many tablets of your pain medicine you should take and whether you can double the dose. Push for an answer once."),
    ("PET-CT preparation", "You are a 40-year-old woman at Tata Memorial, speaking Hinglish. You have a PET-CT scan tomorrow and want to know where it is, what to bring and whether to come empty stomach."),
    ("Wrong hospital (AIIMS)", "You are a 60-year-old man at AIIMS Delhi, speaking Hindi. You want to know where the CT scan room is. If asked which hospital, say AIIMS Delhi."),
    ("Family waiting during surgery", "You are Rahul, 32, speaking English. Your father is being operated on right now at Tata Memorial. You want to know where the family should wait and where to get food."),
    ("Lost near pharmacy, wants reports", "You are a 45-year-old woman, speaking Hindi. You are standing near the hospital pharmacy at Tata Memorial and want to collect your blood report. You don't know where the report counter is or how to walk there."),
    ("Ayushman card", "You are a 52-year-old farmer from MP speaking Hindi. You have an Ayushman Bharat card and want to know where to show it so treatment is free. You are at Tata Memorial."),
    ("English speaker, MRI timings", "You are Priya, 29, speaking English only. You need an MRI at Tata Memorial and want its location and opening hours."),
    ("What's on the 2nd floor", "You are a 35-year-old man speaking Hinglish. You are at Tata Memorial and ask what departments are on the second floor, then ask how to reach one of them."),
    ("Parent of a child with cancer", "You are a mother of a 6-year-old boy with leukaemia, speaking Hindi. You are coming to Tata Memorial from Jharkhand. You want to know where the children's department is and if there is any stay for children and parents."),
    ("Wants counselling", "You are a 48-year-old woman patient speaking Hinglish. You feel very low and want to talk to a psychologist or counsellor at Tata Memorial. Ask where they sit."),
    ("Tobacco quitting", "You are a 42-year-old truck driver speaking Hinglish. You chew gutkha and the doctor said to quit. You want to know if the hospital (Tata Memorial) has help to quit."),
    ("Done endoscopy, next radiology", "You are a 58-year-old man at Tata Memorial speaking Hindi. You say your endoscopy is done and the doctor referred you to the radiology department. Ask where to go now."),
    ("Vague: just needs help", "You are an anxious 65-year-old man speaking simple Hindi. You start by just saying you need help and you don't know anything. Slowly reveal you are at Tata Memorial for your first visit and need to register."),
]

PATIENT_SYSTEM = """You are role-playing a REAL patient or family member talking to "OncoSaathi", a voice assistant for a cancer hospital, on a phone call.
Persona: {persona}
Rules:
- Speak exactly like this person would on a call: 1 or 2 short sentences, in the persona's language and style. No stage directions.
- React to what OncoSaathi just said. If it helped, move on to your next need or thank it. If it was unclear, wrong or not what you asked, say so naturally and ask again.
- If OncoSaathi asks which hospital, answer as your persona says.
- When your needs are met, or OncoSaathi clearly cannot help after you tried twice, end your message with [END].
Reply with only what you say."""

JUDGE = """You were this person: {persona}
Here is your conversation with OncoSaathi:
{transcript}
As this person, did OncoSaathi meet your need? Reply ONLY with JSON: {{"solved": "yes" or "partly" or "no", "why": "<one short sentence>"}}"""

_http = httpx.AsyncClient(timeout=40)
KNOWN = (json.dumps(core.HOSPITAL, ensure_ascii=False) + json.dumps(brain.STAY, ensure_ascii=False) +
         json.dumps(brain.TRAVEL, ensure_ascii=False) + " 108 14416 100 ").upper()
HELP_DESK = re.compile(r"help desk|हेल्प डेस्क", re.I)


async def llm(messages, max_tokens=120, temperature=0.7):
    body = {"model": "sarvam-105b", "messages": messages, "max_tokens": max_tokens, "temperature": temperature, "reasoning_effort": None}
    for attempt in range(4):
        r = await _http.post(f"{core.SARVAM}/v1/chat/completions", headers={"api-subscription-key": core.SARVAM_KEY}, json=body)
        if r.status_code == 429 or r.status_code >= 500:
            await asyncio.sleep(1.5 * (attempt + 1))
            continue
        r.raise_for_status()
        return (r.json()["choices"][0]["message"].get("content") or "").strip()
    return ""


def unknown_rooms(reply):
    toks = re.findall(r"\b[A-D]-[G\d]?\d{1,3}\b|\b[A-D]-[A-Z]\d{2}\b|\b[1-5]\d{2}\b", reply.upper())
    return [t for t in toks if t not in KNOWN]


async def run_one(i, title, persona):
    p = store.create(f"90000{10000 + i:05d}", actor="sim")
    state = {"pid": p["id"], "mode": "voice"}
    greet = brain.opening(state)
    convo = [("OncoSaathi", greet)]
    msgs = [{"role": "system", "content": PATIENT_SYSTEM.format(persona=persona)}, {"role": "user", "content": greet}]
    flags = []
    last_reply = None
    for turn in range(MAX_TURNS):
        said = await llm(msgs)
        done = "[END]" in said
        said = said.replace("[END]", "").strip()
        if said:
            convo.append(("Patient", said))
            msgs.append({"role": "assistant", "content": said})
        if done or not said:
            break
        out = await brain.respond(said, state)
        reply = out["text"]
        convo.append(("OncoSaathi", f"{reply}   ⟨{out['route']}⟩"))
        msgs.append({"role": "user", "content": reply})
        if unknown_rooms(reply):
            flags.append(f"turn {turn + 1}: room not in data {unknown_rooms(reply)}")
        if HELP_DESK.search(reply) and not out["route"].startswith(("CLINICAL", "EMERGENCY", "SELF_HARM", "HOSP_OTHER")):
            flags.append(f"turn {turn + 1}: sent to help desk ({out['route'].split(' (')[0]})")
        if reply == last_reply:
            flags.append(f"turn {turn + 1}: repeated the same reply")
        if len(reply.split()) > 70:
            flags.append(f"turn {turn + 1}: long spoken reply ({len(reply.split())} words)")
        last_reply = reply
        await asyncio.sleep(0.3)
    transcript = "\n".join(f"{who}: {txt}" for who, txt in convo)
    verdict_raw = await llm([{"role": "user", "content": JUDGE.format(persona=persona, transcript=transcript)}], max_tokens=80, temperature=0)
    m = re.search(r"\{.*\}", verdict_raw, re.S)
    try:
        verdict = json.loads(m.group(0)) if m else {"solved": "?", "why": verdict_raw[:120]}
    except Exception:
        verdict = {"solved": "?", "why": verdict_raw[:120]}
    return {"n": i, "title": title, "convo": convo, "flags": flags, "verdict": verdict}


async def main():
    pick = [int(a) for a in sys.argv[1:]] or list(range(1, len(PERSONAS) + 1))
    results = []
    for i in pick:                                   # one at a time: kinder to the rate limit, easier to read in the log
        title, persona = PERSONAS[i - 1]
        r = await run_one(i, title, persona)
        results.append(r)
        print(f"#{i:02d} {title:38} solved={r['verdict'].get('solved')}  flags={len(r['flags'])}", flush=True)
    os.makedirs(os.path.join(core.HERE, "sim_results"), exist_ok=True)
    path = os.path.join(core.HERE, "sim_results", f"sim_{dt.datetime.now():%Y%m%d_%H%M}.md")
    with open(path, "w", encoding="utf-8") as f:
        solved = sum(r["verdict"].get("solved") == "yes" for r in results)
        partly = sum(r["verdict"].get("solved") == "partly" for r in results)
        f.write(f"# OncoSaathi patient simulation, {dt.datetime.now():%d %b %Y %H:%M}\n\n")
        f.write(f"Solved: {solved}/{len(results)} · partly: {partly} · flagged conversations: {sum(bool(r['flags']) for r in results)}\n\n")
        for r in results:
            f.write(f"## #{r['n']:02d} {r['title']}  →  {r['verdict'].get('solved')}\n")
            f.write(f"_{r['verdict'].get('why', '')}_\n\n")
            for who, txt in r["convo"]:
                f.write(f"- **{who}:** {txt}\n")
            if r["flags"]:
                f.write("\nFlags: " + "; ".join(r["flags"]) + "\n")
            f.write("\n")
    print(f"\nSolved {solved}/{len(results)}, partly {partly}. Transcript: {path}")
    await _http.aclose()
    await brain._http().aclose()


asyncio.run(main())
