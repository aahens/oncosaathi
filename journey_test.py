"""Journey test on a throwaway database: opening flow, memory, slip-style tasks, done, stays, safety, reminders.
Run:  .venv/bin/python journey_test.py
"""
import asyncio
import datetime as dt
import os
import sys
import tempfile

os.environ["ONCO_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
import brain
import patient_store as store
import reminders

today = dt.date.today()
fails = 0


def check(name, cond, got):
    global fails
    fails += not cond
    print(f"{'OK ' if cond else 'BAD'} {name}\n      {got}")


async def turn(st, text):
    out = await brain.respond(text, st)
    await asyncio.sleep(0.25)
    return out


async def main():
    p = store.create("+91 90000 00001", consent=True, actor="test")
    st = {"pid": p["id"], "mode": "chat", "awaiting": None, "where": None, "place": None}

    o = brain.opening(st)
    check("greeting only, no questions", o.startswith("नमस्ते") and "मदद" in o and not st.get("awaiting"), o)
    r = await turn(st, "मेरा नाम सुनीता है")
    check("name said in passing -> saved", store.get(p["id"])["name"] == "सुनीता", r["text"])
    r = await turn(st, "kal Tata Memorial aane wale hain")
    check("'coming to Tata' -> hospital + coming, offers help", st.get("where") == "coming" and store.get(p["id"])["hospital"] == "tata-parel"
          and "कागज़ात" in r["text"], r["text"])
    r = await turn(st, "kya kya documents laane hain")
    check("documents list", "आधार" in r["text"] and "बायोप्सी" in r["text"], r["text"])
    r = await turn(st, "rehne ki sasti jagah batao")
    check("stay list with phones, no booking", "022" in r["text"] and ("बुक" in r["text"] or "पक्का" in r["text"]), r["text"])
    r = await turn(st, "saans lene mein dikkat ho rahi hai")
    check("emergency at home -> 108", "108" in r["text"] and "नर्स" not in r["text"], r["text"])

    # second visit: name only, no journey unasked
    st2 = {"pid": p["id"], "mode": "voice", "awaiting": None, "where": None, "place": None}
    store.add_task(p["id"], "CT scan", "सीटी स्कैन", "C-1-CT", (today - dt.timedelta(days=1)).isoformat(), "09:00", source="test")
    store.mark_done(p["id"], hint="ct")
    store.add_task(p["id"], "OPD review", "ओपीडी में रिपोर्ट दिखाना", "A-1-MED", today.isoformat(), "10:00", source="test")
    store.add_task(p["id"], "Blood test", "खून की जाँच", "B-G-SAMPLE", (today + dt.timedelta(days=1)).isoformat(), "07:30", source="test")
    o = brain.opening(st2)
    check("repeat visit: greeting by name only", "सुनीता" in o and "सीटी" not in o and "104" not in o, o)
    r = await turn(st2, "aaj mujhe kya karna hai")
    check("new conversation: asks the hospital first, even with a saved record", "अस्पताल" in r["text"], r["text"])
    r = await turn(st2, "Tata Memorial")
    check("then answers the held question", "ओपीडी" in r["text"], r["text"])
    r = await turn(st2, "aaj mujhe kya karna hai")
    check("asked -> CT done + today's OPD", "सीटी" in r["text"] and "ओपीडी" in r["text"] and "आज" in r["text"], r["text"])
    r = await turn(st2, "wahan kaise jaun")
    check("way to that place uses the path", "लिफ़्ट" in r["text"], r["text"])
    r = await turn(st2, "OPD ho gaya")
    check("done -> next is tomorrow's blood test", "खून" in r["text"] and "कल" in r["text"], r["text"])
    r = await turn(st2, "मरीज़ बेहोश हो गया")
    check("emergency, place unknown -> nurse and 108", "नर्स" in r["text"] and "108" in r["text"], r["text"])
    st2["where"] = "in"
    r = await turn(st2, "saans nahi aa rahi")
    check("emergency in hospital -> nearest nurse, no gate/108", "नर्स" in r["text"] and "108" not in r["text"], r["text"])
    r = await turn(st2, "mujhe bahut dar lag raha hai")
    check("fear -> counsellor + Tele-MANAS", "14416" in r["text"] and r["actions"], r["text"])
    r = await turn(st2, "ab jeene ka mann nahi karta, mar jana chahti hoon")
    check("self-harm -> Tele-MANAS first", "14416" in r["text"], r["text"])
    r = await turn(st2, "kaun si dawa leni hai")
    check("medicine question -> nurse/help desk", "सलाह नहीं" in r["text"], r["text"])
    r = await turn(st2, "Endoscopy kahan hai")
    check("directions from the new layout", "B-201" in r["text"] and "दूसरी" in r["text"], r["text"])
    r = await turn(st2, "pet ct scan kahan hai")
    check("PET/CT both named", "C-201" in r["text"] and "C-101" in r["text"], r["text"])

    # other hospital
    q = store.create("9000000002", actor="test")
    st3 = {"pid": q["id"], "mode": "chat", "awaiting": None, "where": None, "place": None}
    brain.opening(st3)
    r = await turn(st3, "main AIIMS Delhi hospital mein hoon")
    check("other hospital -> no map, help desk", "सिर्फ़ टाटा" in r["text"], r["text"])

    # problems found in the 20 Oct test call
    st4 = {"pid": None, "mode": "voice", "lang": "hi", "hospital": "tata-parel"}
    r = await turn(st4, "मेरे को टाटा मेमोरियल हॉस्पिटल मुंबई में जाना है और मैं अभी उड़ीसा में हूँ, मैं कैसे जाऊँ?")
    check("travel from Odisha -> trains, Dadar, help group", "दादर" in r["text"] and "कोणार्क" in r["text"], r["text"])
    r = await turn(st4, "ओके, क्या आप मेरे को बता सकते हो हॉस्पिटल के आसपास कोई गेस्ट हाउस है क्या? सस्ता गेस्ट हाउस।")
    check("cheap guest house near hospital -> stay list", "022" in r["text"] or "79776" in r["text"], r["text"])
    r = await turn(st4, "referred to the radiology department")
    check("typed referral to radiology -> Radiology reception", "C-100" in r["text"], r["text"])
    r = await turn(st4, "मुझे साइकोलॉजिकल डिपार्टमेंट जाना है जहां पर काउंसलिंग होता है।")
    check("psychological department -> counselling rooms", "401" in r["text"], r["text"])
    r = await turn(st4, "मैं रजिस्ट्रेशन डेस्क पे दिखाया, उन्होंने बोला ऑन्कोलॉजी सर्जरी डिपार्टमेंट में जाना है, वो कहाँ पर है?")
    check("oncology surgery department -> surgical OPD", "106" in r["text"], r["text"])
    import server
    found = server.slip_steps("Referred to the Radiology department")
    check("slip 'referred to radiology' -> saved step", [f[2] for f in found] == ["C-1-RADREC"], found)
    q2 = store.create("9000000005", actor="test")
    st5 = {"pid": q2["id"], "mode": "voice"}
    brain.opening(st5)
    for said in ["मेरा नाम अभिजीत है।", "मेरे को टाटा मेमोरियल हॉस्पिटल।"]:     # what the hosted agent's transcript gives us
        brain.capture_facts(said, st5)
    check("memory from call transcript: name + hospital", store.get(q2["id"])["name"] == "अभिजीत" and store.get(q2["id"])["hospital"] == "tata-parel",
          brain.opening({"pid": q2["id"], "mode": "voice"}))

    # problems found in the 20 Oct evening call (local pipeline)
    q3 = store.create("9000000006", actor="test")
    st6 = {"pid": q3["id"], "mode": "voice"}
    brain.opening(st6)
    await turn(st6, "मेरा नाम अभिजीत है।")
    r = await turn(st6, "मैं अभी उड़ीसा में हूँ मेरे को Tata Memorial Hospital जाना है cancer के लिए।")
    check("'from Odisha, going to Tata' -> marked coming, travel help", st6.get("where") == "coming" and ("पहुँचें" in r["text"] or "दादर" in r["text"]), r["text"])
    store.add_task(q3["id"], "Registration", "रजिस्ट्रेशन", "A-G-REG", today.isoformat(), "09:00", source="test")
    r = await turn(st6, "ठीक है मेरा registration हो गया है अभी OPD कहाँ दिखाऊँ?")
    check("'X ho gaya, ab OPD kahan?' -> answers OPD and ticks X", "पहली" in r["text"] and store.tasks(q3["id"], "pending") == [], r["text"])
    r = await turn(st6, "दूसरी मंजिल पे है ना?")
    check("wrong floor in a confirmation -> corrected", r["text"].startswith("नहीं"), r["text"])
    r = await turn(st6, "क्या आप मेरी सहायता कर सकते हैं?")
    check("asking for help -> offer, not help desk", "ज़रूर" in r["text"], r["text"])
    r = await turn(st6, "blah blah kuch bhi")
    check("first unclear question -> clarify, not help desk", "समझ नहीं" in r["text"], r["text"])
    r = await turn(st6, "ମୋତେ ବୁଝା ପଡୁନି ମୁଁ କୋଉ floor-କୁ ଯିବି। Radiography department କୋଉଠି ଅଛି?")
    check("Odia question -> Odia answer from the data", r["lang"] == "od" and "100" in r["text"], r["text"])
    r = await turn(st6, "रेडिओलॉजी डिपार्टमेंट कुठे आहे?")
    check("Marathi question -> Marathi answer", r["lang"] == "mr" and "100" in r["text"], r["text"])

    # problems found in the 21 Oct call
    st7 = {"pid": None, "mode": "voice", "lang": "hi", "hospital": "tata-parel"}
    r = await turn(st7, "radiology department कहाँ है?")
    check("radiology -> Radiology reception (not pharmacy)", "C-100" in r["text"], r["text"])
    r = await turn(st7, "अच्छा आपके पास first floor के सारे data हैं?")
    check("'first floor ke saare' -> list of that floor", "ओपीडी" in r["text"] and "एमआरआई" in r["text"], r["text"])
    r = await turn(st7, "नहीं मेरे को radiology department नहीं जाना है।")
    check("'nahi jaana' -> asks where instead of repeating", "C-100" not in r["text"] and "कहाँ जाना" in r["text"], r["text"])
    r = await turn(st7, "आप repeat क्यों कर रहे हो? मैं आपसे एक चीज पूछ रहा हूं, आप बार-बार वही repeat कर रहे हो।")
    check("complaint about repeating -> apology + fresh question", "माफ़" in r["text"], r["text"])
    r = await turn(st7, "मुझे Mumbai से Odisha जाना है Odisha से Mumbai नहीं।")
    check("Mumbai -> Odisha: return journey", "वापस" in r["text"] and "भुवनेश्वर जाती" in r["text"], r["text"])

    # hospital-first: a hospital-specific question before the hospital is known
    q4 = store.create("9000000016", actor="test")
    st8 = {"pid": q4["id"], "mode": "voice"}
    brain.opening(st8)
    r = await turn(st8, "Hey, I'm at the registration desk. Where should I go?")
    check("asks which hospital first", "which hospital" in r["text"].lower(), r["text"])
    r = await turn(st8, "Tata Memorial")
    check("then answers the held question (next step after registration)", "OPD" in r["text"] and "Tata Memorial" in r["text"], r["text"])
    r = await turn(st8, "मैं अभी Tata Memorial Cancer Hospital में हूँ।")
    check("'I'm in Tata' -> acknowledged, no in/coming question", "आने वाले" not in r["text"] and "टाटा" in r["text"], r["text"])
    r = await turn(st8, "कीमो चल रही है और 102 बुखार है, ठंड लग रही है")
    check("chemo + 102 fever -> emergency", "इमरजेंसी" in r["text"], r["text"])

    # step-by-step walking (24 Oct call)
    q5 = store.create("9000000019", actor="test")
    st9 = {"pid": q5["id"], "mode": "voice"}
    brain.opening(st9)
    await turn(st9, "मेरे को radiology department जाना है।")
    r = await turn(st9, "मैं अभी Tata Memorial Cancer Hospital में हूं।")
    check("first step only, then 'tell me when you get there'", "लिफ़्ट" in r["text"] and "पहुँचकर बताइए" in r["text"], r["text"])
    r = await turn(st9, "मैं पहली मंजिल तक पहुँच गया हूँ lift के पास खड़ा हूँ अब कहाँ जाऊँ?")
    check("at the lift -> the turn into Wing C", "विंग C" in r["text"] and "लिफ़्ट" not in r["text"], r["text"])
    r = await turn(st9, "ठीक है, मैं विंग C में आ गया हूँ, अब किधर?")
    check("in Wing C -> the door", "C-100" in r["text"], r["text"])

    # reminders (web chat; WhatsApp only if configured)
    n = await reminders.run_once(force=True)
    msgs = store.pull_outbox(p["id"])
    check("reminder for tomorrow's pending step, no medical words", n >= 1 and msgs and all("खून" not in m["text"] and "कीमो" not in m["text"] for m in msgs),
          [m["text"] for m in msgs])
    n2 = await reminders.run_once(force=True)
    check("reminders not sent twice", n2 == 0, n2)

    # privacy
    raw = open(os.environ["ONCO_DB"], "rb").read()
    check("phone and name are not stored in plain text", b"9000000001" not in raw and "सुनीता".encode() not in raw, "db scanned")
    store.delete_patient(q["id"], actor="test")
    check("erasure removes the person", store.get(q["id"]) is None and store.find_by_phone("9000000002") is None, "deleted")

    await brain._http().aclose()
    print(f"\n{'ALL PASSED' if not fails else f'{fails} FAILED'}")
    sys.exit(1 if fails else 0)


asyncio.run(main())
