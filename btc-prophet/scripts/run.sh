#!/bin/bash
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."
exec .venv/bin/python -m uvicorn prophet.app:app --host 127.0.0.1 --port 8000 --workers 1
