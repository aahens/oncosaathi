"""Appointment reminders: the evening before and the morning of each dated task.

Wording is deliberately non-medical (time + place only, no department or treatment name), because the message passes
through WhatsApp. Always delivered to the web chat; also sent on WhatsApp when the WhatsApp Cloud API is configured in
.env (WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_TEMPLATE, WHATSAPP_TEMPLATE_LANG). The template must be
approved by Meta first, as a "utility" template with two body variables, e.g.:
    "नमस्ते! OncoSaathi याद दिला रही है: आपका अपॉइंटमेंट {{1}} है, {{2}}। सवाल हो तो इस नंबर पर लिखिए।"
"""
import asyncio
import datetime as dt
import os

import httpx

import brain
import patient_store as store

CHECK_EVERY_S = 300
MORNING_HOUR, EVENING_HOUR = 7, 18


def whatsapp_ready():
    return all(os.environ.get(k, "").strip() for k in ("WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_TEMPLATE"))


async def send_whatsapp(phone, params):
    """Send the approved template to +91XXXXXXXXXX. Returns 'sent' or an error string (never raises)."""
    url = f"https://graph.facebook.com/v21.0/{os.environ['WHATSAPP_PHONE_NUMBER_ID'].strip()}/messages"
    body = {"messaging_product": "whatsapp", "to": phone.lstrip("+"), "type": "template",
            "template": {"name": os.environ["WHATSAPP_TEMPLATE"].strip(),
                         "language": {"code": os.environ.get("WHATSAPP_TEMPLATE_LANG", "hi").strip() or "hi"},
                         "components": [{"type": "body", "parameters": [{"type": "text", "text": p} for p in params]}]}}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(url, json=body, headers={"Authorization": f"Bearer {os.environ['WHATSAPP_TOKEN'].strip()}"})
        return "sent" if r.status_code < 300 else f"error {r.status_code}: {r.text[:120]}"
    except Exception as e:
        return f"error {e!r}"


def reminder_text(task, lang="hi"):
    when = brain._when(task, lang)
    u = brain.PLACES.get(task.get("unit_id"))
    where = brain.place_short(u, lang) if u else ("अस्पताल" if lang == "hi" else "the hospital")
    text = (f"⏰ याद दिलाना: आपका अपॉइंटमेंट {when} है, {where}।" if lang == "hi" else
            f"⏰ Reminder: your appointment is {when}, {where}.")
    return text, [when, where]


async def remind(task, kind):
    text, params = reminder_text(task)
    store.push_outbox(task["patient_id"], text)
    channel, status = "webchat", "delivered"
    if whatsapp_ready():
        phone = store.get_phone(task["patient_id"], actor="reminders")
        status = await send_whatsapp(phone, params) if phone else "no phone"
        channel = "webchat+whatsapp"
    store.record_reminder(task["id"], kind, channel, status)
    store.audit("reminders", task["patient_id"], "reminder", f"{kind} via {channel}: {status}")
    return status


async def run_once(force=False):
    """Send whatever is due now. force=True sends today's and tomorrow's reminders regardless of the hour (demo button)."""
    now = dt.datetime.now()
    today, tomorrow = now.date().isoformat(), (now.date() + dt.timedelta(days=1)).isoformat()
    sent = 0
    if force or now.hour >= MORNING_HOUR:
        for t in store.due_for_reminder(today):
            if not store.reminder_sent(t["id"], "day_of"):
                await remind(t, "day_of")
                sent += 1
    if force or now.hour >= EVENING_HOUR:
        for t in store.due_for_reminder(tomorrow):
            if not store.reminder_sent(t["id"], "day_before"):
                await remind(t, "day_before")
                sent += 1
    return sent


async def loop():
    while True:
        try:
            await run_once()
        except Exception as e:
            print(f"[reminders] {e!r}", flush=True)
        await asyncio.sleep(CHECK_EVERY_S)
