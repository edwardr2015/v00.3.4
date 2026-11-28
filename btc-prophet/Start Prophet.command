#!/bin/bash
set -euo pipefail
umask 077
cd "$(dirname "$0")"
trap 'echo "Startup stopped. Read the error above, then press Enter to close."; read -r' ERR
if ! command -v python3 >/dev/null; then
  echo "Install Python 3.12 from https://www.python.org/downloads/macos/ and try again."
  exit 1
fi
python3 -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11 or newer is required"'
export PROPHET_DB="${PROPHET_DB:-$PWD/data/timing/prophet.sqlite3}"
export PROPHET_HISTORY_DB="${PROPHET_HISTORY_DB:-$PWD/data/timing/history.sqlite3}"
# Early exit is the owner's setting; a restart must never silently switch to holding.
export PROPHET_PRIMARY_EXIT="${PROPHET_PRIMARY_EXIT:-probability}"
if [ "${1:-}" = "--rollback" ]; then
  python3 - <<'PY_ROLLBACK'
import os
from pathlib import Path
folder=Path(os.environ["PROPHET_DB"]).parent/"updates"
(folder/"next-release.json").unlink(missing_ok=True)
PY_ROLLBACK
elif [ "${PROPHET_STAGED_LAUNCH:-0}" != "1" ]; then
  staged=$(python3 - <<'PY_STAGE'
import hashlib
import json
import os
from pathlib import Path

updates=Path(os.environ["PROPHET_DB"]).parent.resolve()/"updates"
pointer=updates/"next-release.json"
if not pointer.is_file():
    raise SystemExit(0)
try:
    receipt=json.loads(pointer.read_bytes())
    if receipt is None:
        raise SystemExit(0)
    root=(updates/receipt["directory"]).resolve()
    if root.parent!=updates or not root.name.startswith("ai-btc-prophet-"):
        raise ValueError("invalid staged folder")
    archive=updates/(receipt["sha256"]+".zip")
    if hashlib.sha256(archive.read_bytes()).hexdigest()!=receipt["sha256"]:
        raise ValueError("staged archive checksum changed")
    from zipfile import ZipFile
    with ZipFile(archive) as release:
        for member in release.infolist():
            if member.is_dir(): continue
            name=Path(member.filename).parts[1:]
            target=(root.joinpath(*name)).resolve()
            if not target.is_relative_to(root) or target.read_bytes()!=release.read(member):
                raise ValueError("staged source checksum changed")
    print(root/"Start Prophet.command")
except (OSError,KeyError,ValueError,TypeError) as exc:
    import sys
    print("Staged update could not be verified. The installed version will start.",file=sys.stderr)
PY_STAGE
  )
  if [ -n "$staged" ]; then
    export PROPHET_STAGED_LAUNCH=1
    exec bash "$staged"
  fi
fi

if [ ! -x .venv/bin/python ]; then python3 -m venv .venv; fi
if ! .venv/bin/python - <<'PY_CHECK'
from importlib.metadata import PackageNotFoundError, version
from importlib.util import find_spec
from pathlib import Path
import sys
import tomllib

expected = tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"]
try:
    ready = version("ai-btc-prophet") == expected and all(
        find_spec(module) for module in ("fastapi", "uvicorn", "httpx", "openpyxl", "websockets")
    )
except PackageNotFoundError:
    ready = False
sys.exit(0 if ready else 1)
PY_CHECK
then
  echo "Installing Prophet's required packages. First setup or upgrades need internet access."
  .venv/bin/python -m pip install -e .
fi
export PROPHET_DB="${PROPHET_DB:-$PWD/data/timing/prophet.sqlite3}"
export PROPHET_HISTORY_DB="${PROPHET_HISTORY_DB:-$PWD/data/timing/history.sqlite3}"
export PROPHET_FRESH_RESEARCH="${PROPHET_FRESH_RESEARCH:-1}"
if [ "${PROPHET_ENABLE_RESEARCH:-1}" = "1" ]; then
  if ! .venv/bin/python -c 'import sklearn, xgboost, lightgbm' >/dev/null 2>&1; then
    echo "Installing optional research libraries."
    if ! .venv/bin/python -m pip install '.[research]'; then
      echo "Installing competition dependencies independently after the combined install failed."
      for requirement in scikit-learn==1.7.2 xgboost==3.1.1 lightgbm==4.6.0; do
        .venv/bin/python -m pip install "$requirement" || echo "Unavailable: $requirement"
      done
    fi
    if ! .venv/bin/python -c 'import sklearn, xgboost, lightgbm'; then
      echo "Some competition libraries could not load. Check the startup error above and the README."
    fi
  fi
fi
export PROPHET_RESEARCH_MODELS="${PROPHET_RESEARCH_MODELS:-ensemble,random_forest,gradient_boosting,lightgbm,xgboost}"
if [ "${PROPHET_ENABLE_NEURAL:-0}" = "1" ]; then
  if ! .venv/bin/python -c 'import torch' >/dev/null 2>&1; then
    echo "Installing the optional neural model package. This can take a few minutes."
    .venv/bin/python -m pip install '.[neural]'
  fi
  case ",$PROPHET_RESEARCH_MODELS," in
    *,lstm,*) ;;
    *) export PROPHET_RESEARCH_MODELS="$PROPHET_RESEARCH_MODELS,lstm" ;;
  esac
fi
# Phone remote: the file phone-remote-on next to the database (or PROPHET_REMOTE=1) opens
# the dashboard to this Wi-Fi network. A password is then always required.
host=127.0.0.1
if [ "${PROPHET_REMOTE:-0}" = "1" ] || [ -f "$(dirname "$PROPHET_DB")/phone-remote-on" ]; then
  export PROPHET_AUTH_REQUIRED=1
  host=0.0.0.0
  lan_name="$(scutil --get LocalHostName 2>/dev/null || hostname -s)"
  lan_ip="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
  export PROPHET_HOSTS="${PROPHET_HOSTS:+$PROPHET_HOSTS,}${lan_name}.local${lan_ip:+,$lan_ip}"
fi
export PROPHET_AUTH_REQUIRED="${PROPHET_AUTH_REQUIRED:-0}"
if [ "$PROPHET_AUTH_REQUIRED" = "1" ] && [ ! -f "$(dirname "$PROPHET_DB")/auth.json" ]; then
  echo "Create the password you will type on your phone."
  .venv/bin/python -m scripts.setup_auth
fi
echo "Open http://127.0.0.1:8000 if the browser does not open. Keep this window open."
if [ "$host" = "0.0.0.0" ]; then
  echo "PHONE (same Wi-Fi): http://${lan_name}.local:8000/remote${lan_ip:+   or   http://$lan_ip:8000/remote}"
fi
(sleep 2; open http://127.0.0.1:8000) &
# The phone's Install now / Restart buttons stop the app with a restart request.
# This launcher then starts again, which opens the newly selected release.
export PROPHET_SUPERVISED=1
# Keep the Mac awake while this window is open (macOS caffeinate; stops when the bot stops).
if command -v caffeinate >/dev/null 2>&1; then caffeinate -ims -w $$ & fi
marker="$(dirname "$PROPHET_DB")/updates/restart-requested"
status=0
.venv/bin/python -m uvicorn prophet.app:app --host "$host" --port 8000 --workers 1 || status=$?
if [ -f "$marker" ]; then
  echo "Restarting BTC Prophet for the update you approved..."
  unset PROPHET_STAGED_LAUNCH
  exec bash "$PWD/Start Prophet.command"
fi
exit "$status"
