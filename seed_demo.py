"""Preload a demo journey for the presentation on DEMO_PATIENT_PHONE (from .env).

Run:   .venv/bin/python seed_demo.py           adds: CT done yesterday, OPD review today 10:00, blood test + chemo day care tomorrow
       .venv/bin/python seed_demo.py --reset   first erases that number's record (name, steps), so the demo starts from "What's your name?"
Dates are relative to the day you run it, so run it on the morning of the demo.
"""
import datetime as dt
import os
import sys

import patient_store as store

phone = os.environ.get("DEMO_PATIENT_PHONE", "")
if not store.normalize_phone(phone):
    sys.exit("Set DEMO_PATIENT_PHONE in .env to a 10-digit Indian mobile number.")

if "--reset" in sys.argv:
    old = store.find_by_phone(phone, actor="seed")
    if old:
        store.delete_patient(old["id"], actor="seed")
        print("Erased the old record for the demo number.")

p = store.create(phone, consent=True, actor="seed")
today = dt.date.today()
d = lambda n: (today + dt.timedelta(days=n)).isoformat()

store.delete_tasks(p["id"], "demo", actor="seed")             # re-runs replace the demo steps instead of stacking them

ct = store.add_task(p["id"], "CT scan", "सीटी स्कैन", "C-1-CT", d(-1), "09:00", source="demo", actor="seed")
store.mark_done(p["id"], task_id=ct, actor="seed")
store.add_task(p["id"], "OPD review with reports", "ओपीडी में रिपोर्ट दिखाना", "A-1-MED", d(0), "10:00", source="demo", actor="seed")
store.add_task(p["id"], "Blood test", "खून की जाँच", "B-G-SAMPLE", d(1), "07:30", source="demo", actor="seed")
store.add_task(p["id"], "Chemotherapy Day Care", "कीमो डे केयर", "A-2-DC", d(1), "08:30", source="demo", actor="seed")

name = store.get(p["id"], actor="seed").get("name")
print(f"Demo journey ready for the demo number (name on file: {name or 'none yet, the app will ask'}).")
print("Sign in at the chat with that number, then ask: 'aaj mujhe kya karna hai?'")
