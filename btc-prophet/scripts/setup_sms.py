"""Run locally after creating an SMS provider account. Never paste credentials into chat."""

import getpass
import json
import os
import re
from pathlib import Path

root = Path(__file__).resolve().parents[1] / "data"
root.mkdir(exist_ok=True)
try:
    recipient = json.loads((root / "alerts.json").read_text()).get("to", "")
except (OSError, ValueError):
    recipient = ""
print("Optional Twilio risk alerts. Provider charges may apply. No message is sent by setup.")
sid = input("Account SID (AC...): ").strip()
token = getpass.getpass("Auth token (hidden): ").strip()
sender = input("Twilio SMS sender number, e.g. +1...: ").strip()
to = (
    input(
        "Your destination number with country code"
        + (" [press Enter to keep saved number]" if recipient else "")
        + ": "
    ).strip()
    or recipient
)
if not re.fullmatch(r"AC[0-9a-fA-F]{32}", sid) or not token:
    raise SystemExit("Invalid account settings")
if not all(re.fullmatch(r"\+[1-9][0-9]{7,14}", v) for v in (sender, to)):
    raise SystemExit("Use international +country-code phone numbers")
path = root / "sms.json"
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump(dict(sid=sid, token=token, sender=sender, to=to), f)
os.chmod(path, 0o600)
print(
    "Saved private local settings. Open Settings in the dashboard and explicitly send a test; confirm delivery."
)
