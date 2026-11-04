# OncoSaathi demo agent

A patient sends a WhatsApp voice note ("CBC test kahan hoga?"). Sarvam Saaras turns it into text, `router.py` picks a fixed, approved answer (medical questions always get "speak to your doctor"), and Sarvam Bulbul speaks the answer back as a WhatsApp voice note.

## 1. Run it locally (no WhatsApp needed)

```powershell
pip install -r requirements.txt
python test_router.py              # offline routing and data checks, no API calls
python -m uvicorn main:app --reload
```

Open http://127.0.0.1:8000, hold the microphone button and ask a question. Or open `requests.http` and click "Send Request" (needs the REST Client extension). In VS Code you can also press F5 and pick **OncoSaathi server (uvicorn)**.

## 2. Connect WhatsApp (Cloud API)

1. Go to developers.facebook.com, create an app of type **Business**, and add the **WhatsApp** product.
2. Open WhatsApp > API Setup. Copy the **temporary access token** into `WA_ACCESS_TOKEN` and the **Phone number ID** into `WA_PHONE_NUMBER_ID` in `.env`. Add your own phone as a test recipient.
3. Copy App settings > Basic > **App secret** into `WA_APP_SECRET`.
4. Give your laptop a public HTTPS address: install ngrok, then run `ngrok http 8000`.
5. Restart the server so it reads the new `.env` values.
6. In WhatsApp > Configuration > Webhook, set the callback URL to `https://<your-ngrok-address>/webhook` and the verify token to the value of `WA_VERIFY_TOKEN` in `.env`. Click Verify and save, then subscribe to the **messages** field.
7. From your phone, send a voice note to the test number. The bot replies with the answer as text and as a voice note.

The temporary token expires after 24 hours. Use a System User token for anything longer.

## Files

| File | Purpose |
|---|---|
| `main.py` | FastAPI server: browser demo, `/navigate-voice`, `/navigate-text`, WhatsApp `/webhook` |
| `sarvam_voice.py` | Sarvam speech-to-text, text-to-speech (OGG/Opus, no ffmpeg) and label-only intent classifier |
| `router.py` | Emergency check, then clinical guardrail, then place lookup. All replies are fixed sentences built from the data file |
| `hospital_tmh.json` | Tata Memorial buildings, floors and places, each with its booklet section. To add a hospital, write another file like this |
| `demo.html` | Microphone page and campus map, served at `/` |
| `test_router.py` | Offline routing tests |
| `sarvam_chat.py` | One-call Sarvam check |

## Before the pilot

- Locations come from the public TMH Patient Booklet 2025 and are not verified or endorsed by the hospital. Get them checked by TMH before any patient uses them.
- The map shows buildings and floors only. Turn-by-turn directions need the hospital's real 2D floor plans.
- The Marathi replies and the emergency reply need checking by a native speaker and the NCG mentors.
- Patient phone numbers are only used to send the reply. They are never logged or stored.
