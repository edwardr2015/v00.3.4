"""Run on your Mac to install a private per-user launchd background service."""

import os
import plistlib
import subprocess
import sys
from pathlib import Path

if sys.platform != "darwin":
    raise SystemExit("Run this installer on your Mac, not the Cloud computer.")
root = Path(__file__).resolve().parents[1]
python = root / ".venv/bin/python"
if not python.exists():
    raise SystemExit("Run Start Prophet.command once to install dependencies first.")
dest = Path.home() / "Library/LaunchAgents/com.btcprophet.paper.plist"
dest.parent.mkdir(parents=True, exist_ok=True)
logs = root / "data/logs"
logs.mkdir(parents=True, exist_ok=True)
config = dict(
    Label="com.btcprophet.paper",
    ProgramArguments=[
        str(python),
        "-m",
        "uvicorn",
        "prophet.app:app",
        "--host",
        "127.0.0.1",
        "--port",
        "8000",
        "--workers",
        "1",
    ],
    WorkingDirectory=str(root),
    RunAtLoad=True,
    KeepAlive=True,
    ThrottleInterval=15,
    StandardOutPath=str(logs / "service.log"),
    StandardErrorPath=str(logs / "service-error.log"),
)
dest.write_bytes(plistlib.dumps(config))
domain = f"gui/{os.getuid()}"
subprocess.run(["launchctl", "bootout", domain, str(dest)], check=False, capture_output=True)
subprocess.run(["launchctl", "bootstrap", domain, str(dest)], check=True)
print(
    "Installed:",
    dest,
    "\nDashboard: http://127.0.0.1:8000\nA restart always stops entries for manual review.",
)
