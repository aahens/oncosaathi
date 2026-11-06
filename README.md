# OncoSaathi

A Hindi-first voice companion that guides cancer patients and their families through a hospital, one step at a time.
Built for KOITA Health-a-thon 2026 by Team Oslava.

**Try it:** https://oncosaathi.onrender.com
Open it in Chrome, sign in with a mobile number, and press the call button (allow the microphone).
The first visit after a quiet spell can take about a minute while the free server wakes up.

> Buildings, floors and counters come from the public **TMH Patient Booklet 2025** (not verified by the hospital). The booklet
> has no map, so where the buildings stand and where a room sits inside a floor are drawn by us and marked illustrative.
> OncoSaathi gives directions and logistics only. It never gives medical advice: emergencies go to the nearest nurse or help desk,
> and distress to Tele-MANAS 14416.

## What it does
- Voice call in Hindi, English and 9 more Indian languages, with interruption
- Directions to the booklet's departments across the 5 buildings (MB, HBB, GJB, AB, SB), with the separate locations for general and private patients; departments the booklet doesn't list are said to be unlisted, never guessed
- A walkthrough map after each direction, with a line drawn from where the patient is standing to the room (chat, call screen and WhatsApp). Patients say where they are ("मैं फ़ार्मेसी पर हूँ") or pick it in the chat; until then the line starts at the main gate and the caption says so
- Reads a photo of the doctor's referral slip and names the right department
- Real places to stay near the hospital (phone numbers only, never books), and travel from other states
- Remembers the patient with consent; the data can be deleted at any time

## How it works
Speech (Sarvam Saaras v4) → safety rules first → Sarvam-105b picks a label only → answer filled from the hospital
data files (`hospital.json`, `journey.json`, `stay.json`, `travel.json`) → speech (Sarvam Bulbul v3).
The language model never writes room numbers itself, so it cannot invent them.

Calls can also run on the team's hosted Sarvam Samvaad agent (`CALL_ENGINE=sarvam_agent`, see `samvaad_call.py`), which handles
interruptions well. Its instructions are generated from the same hospital data by `build_agent_prompt.py` into
`agent_prompt_new_agent.md`. During and after such a call, the route maps and written directions still come from our own data.

## Run it yourself
```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env    # then add your own keys
CALL_ENGINE=local .venv/bin/python server.py
```
Open http://127.0.0.1:7860. On Windows: `pip install -r requirements.txt`, then `python server.py`.

The hospital data is written by `build_hospital.py` from the booklet table inside it: edit that table, then run `python build_hospital.py` and `python make_floor_plans.py`. `route_map.py` draws the campus, each building floor and the route maps; `/map` on the server shows them all. The earlier voice-note demo is in `old_demo/`.

## WhatsApp
Set `WHATSAPP_PROVIDER=meta` and the `WHATSAPP_` keys in `.env`, then point Meta's webhook at `<public link>/whatsapp/meta` and subscribe to `messages`. Run `python keep_link.py` beside `server.py`: it makes a free public link, tells Meta the address, and makes a new one whenever the link dies. A Twilio trial account receives messages but does not deliver the replies.

## Tests
`route_test.py` (walkthrough map, no API calls), `meta_test.py` and `wa_test.py` (WhatsApp with Meta or Twilio mocked),
`journey_test.py` (journey, memory, consent, deletion), `eval_brain.py` (questions → right place),
`call_test.py` (spoken end-to-end call), `web_agent_test.py` and `wa_agent_test.py` (calls through the hosted agent, with maps),
`simulate_patients.py` (20 simulated patients).

## Deployment
`render.yaml` deploys this repository on Render. Keys go in a Render Secret File named `.env`. Every push to `main` updates the live link automatically.
Keys are set in Render's dashboard and are never stored in this repository.
