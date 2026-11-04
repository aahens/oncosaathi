"""One-time WhatsApp number setup on Meta. Run: python wa_setup.py

1. Profile photo: static/logo.png on a white square, uploaded with Meta's Resumable Upload API.
2. Calling: switched on, so the 📞 button appears in the patient's WhatsApp (answered by wa_call.py).
3. Prints the number's name. The display name of Meta's free test number ("Test Number") can't be changed; a real
   number gets "OncoSaathi" after Meta reviews the name.
Uses WHATSAPP_TOKEN and WHATSAPP_PHONE_NUMBER_ID from .env, and prints no secrets.
"""
import io
import os

import httpx
from dotenv import load_dotenv
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))
GRAPH = f"https://graph.facebook.com/{os.environ.get('WHATSAPP_GRAPH_VERSION', 'v23.0').strip() or 'v23.0'}"
TOKEN = os.environ["WHATSAPP_TOKEN"].strip()
PHONE_ID = os.environ["WHATSAPP_PHONE_NUMBER_ID"].strip()
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def logo_jpeg():
    """640x640 JPEG with margin, so WhatsApp's round crop doesn't cut the ribbon."""
    with Image.open(os.path.join(HERE, "static", "logo.png")) as im:
        logo = im.convert("RGB").resize((500, 500), Image.LANCZOS)
    out = Image.new("RGB", (640, 640), "white")
    out.paste(logo, (70, 70))
    buf = io.BytesIO()
    out.save(buf, "JPEG", quality=92)
    return buf.getvalue()


def set_photo(c):
    app_id = os.environ.get("WHATSAPP_APP_ID", "").strip() or c.get(f"{GRAPH}/app", headers=AUTH).raise_for_status().json()["id"]
    data = logo_jpeg()
    up = c.post(f"{GRAPH}/{app_id}/uploads", headers=AUTH,
                params={"file_name": "oncosaathi_logo.jpg", "file_length": len(data), "file_type": "image/jpeg"})
    up.raise_for_status()
    h = c.post(f"{GRAPH}/{up.json()['id']}", headers={"Authorization": f"OAuth {TOKEN}", "file_offset": "0"}, content=data)
    h.raise_for_status()
    r = c.post(f"{GRAPH}/{PHONE_ID}/whatsapp_business_profile", headers=AUTH,
               json={"messaging_product": "whatsapp", "profile_picture_handle": h.json()["h"]})
    print("profile photo:", "set" if r.status_code < 300 else f"failed {r.status_code} {r.text[:300]}")


def enable_calling(c):
    r = c.post(f"{GRAPH}/{PHONE_ID}/settings", headers=AUTH,
               json={"calling": {"status": "ENABLED", "call_icon_visibility": "DEFAULT"}})
    print("calling:", "enabled" if r.status_code < 300 else f"failed {r.status_code} {r.text[:300]}")
    s = c.get(f"{GRAPH}/{PHONE_ID}/settings", headers=AUTH).json().get("calling", {})
    print("  now:", s.get("status"), "| call button:", s.get("call_icon_visibility"))


def show_name(c):
    r = c.get(f"{GRAPH}/{PHONE_ID}", headers=AUTH, params={"fields": "display_phone_number,verified_name,name_status"}).json()
    print(f"number: {r.get('display_phone_number')} | name on WhatsApp: {r.get('verified_name')} ({r.get('name_status')})")


if __name__ == "__main__":
    with httpx.Client(timeout=30) as c:
        set_photo(c)
        enable_calling(c)
        show_name(c)
