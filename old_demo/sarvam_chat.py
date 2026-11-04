"""One Sarvam chat completion call. Run: python sarvam_chat.py"""
import os
from pathlib import Path

from sarvamai import SarvamAI

# Load SARVAM_API_KEY from the .env file next to this script (no extra packages needed).
env_file = Path(__file__).with_name(".env")
if env_file.exists():
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

api_key = os.environ.get("SARVAM_API_KEY")
if not api_key:
    raise SystemExit("SARVAM_API_KEY is empty. Paste your key into .env and run again.")

client = SarvamAI(api_subscription_key=api_key)
response = client.chat.completions(
    model="sarvam-105b-conversations",
    messages=[{"role": "user", "content": "Say hello in Hindi and English in one short line."}],
)
print(response.choices[0].message.content)
