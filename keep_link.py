"""Keeps a public link to this laptop alive for WhatsApp. Run it beside server.py:  python keep_link.py

Cloudflare's free links die after a few hours and come back with a new address. This starts cloudflared, tells Meta the new
address (the WhatsApp number's webhook override), saves it to .env as PUBLIC_URL, and repeats whenever the link stops
answering. server.py needs no restart: Meta's webhook checks use the app secret, and the map and voice note are read locally.
"""
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values

HERE = Path(__file__).parent
ENV_FILE = HERE / ".env"
PORT = os.environ.get("PORT", "7860")
CLOUDFLARED = [shutil.which("cloudflared"), r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
               r"C:\Program Files\cloudflared\cloudflared.exe"]
# a quick tunnel is words-with-hyphens; cloudflared's errors mention https://api.trycloudflare.com, which is not one (3 Nov 2026)
LINK_RX = re.compile(r"https://[a-z0-9]+(?:-[a-z0-9]+)+\.trycloudflare\.com")
CHECK_EVERY_S, FAILS_BEFORE_NEW_LINK = 30, 3


def log(text):
    print(time.strftime("%H:%M:%S"), text, flush=True)


def start_tunnel():
    """Start cloudflared and return (process, its public link)."""
    exe = next((c for c in CLOUDFLARED if c and os.path.exists(c)), None)
    if not exe:
        raise SystemExit("cloudflared is not installed. Run: winget install --id Cloudflare.cloudflared")
    proc = subprocess.Popen([exe, "tunnel", "--url", f"http://localhost:{PORT}"], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    found = []

    def read_output():                                   # keep draining it for as long as the tunnel runs
        for line in proc.stdout:
            m = LINK_RX.search(line)
            if m and not found:
                found.append(m.group(0))

    threading.Thread(target=read_output, daemon=True).start()
    for _ in range(60):
        if found:
            return proc, found[0]
        if proc.poll() is not None:
            break
        time.sleep(1)
    proc.kill()
    raise RuntimeError("cloudflared gave no link")


def answering(link):
    """True if this link reaches the running app. A 502 means the link is fine but server.py is not running."""
    try:
        status = httpx.get(f"{link}/api/config", timeout=20).status_code
    except httpx.HTTPError:
        return False
    if status == 502:
        log("the link works, but server.py is not running. Start it with: python server.py")
    return status < 500 or status == 502


def wait_until_answering(link):
    for _ in range(24):                                  # a new link can take a minute to reach the wider internet
        if answering(link):
            return True
        time.sleep(5)
    return False


def tell_meta(link):
    env = dotenv_values(ENV_FILE)
    version = env.get("WHATSAPP_GRAPH_VERSION") or "v25.0"
    r = httpx.post(f"https://graph.facebook.com/{version}/{env['WHATSAPP_PHONE_NUMBER_ID']}",
                   headers={"Authorization": f"Bearer {env['WHATSAPP_TOKEN']}"}, timeout=30,
                   json={"webhook_configuration": {"override_callback_uri": f"{link}/whatsapp/meta",
                                                   "verify_token": env["WHATSAPP_VERIFY_TOKEN"]}})
    if r.status_code >= 300:
        raise RuntimeError(f"Meta refused the new link: {r.status_code} {r.json().get('error', {}).get('message', '')[:200]}")


def save_link(link):
    text = ENV_FILE.read_text(encoding="utf-8")
    ENV_FILE.write_text(re.sub(r"^PUBLIC_URL=.*$", f"PUBLIC_URL={link}", text, flags=re.M), encoding="utf-8")


def main():
    while True:
        proc = None
        try:
            proc, link = start_tunnel()
            log(f"new link: {link}")
            if not wait_until_answering(link):
                raise RuntimeError("the new link never answered")
            tell_meta(link)
            save_link(link)
            log("Meta updated. WhatsApp replies are on. Keep this window open.")
            fails = 0
            while proc.poll() is None and fails < FAILS_BEFORE_NEW_LINK:
                time.sleep(CHECK_EVERY_S)
                fails = 0 if answering(link) else fails + 1
            log("the link stopped answering, making a new one")
        except KeyboardInterrupt:
            if proc:
                proc.kill()
            return
        except Exception as e:
            log(f"problem: {e}. Trying again in 30 s")
            time.sleep(30)
        if proc:
            proc.kill()


if __name__ == "__main__":
    main()
