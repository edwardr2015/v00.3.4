"""Save your Polymarket US API keys on THIS computer only, then check them (read only).

    .venv/bin/python -m scripts.setup_live

The keys are typed here (nothing shows while typing), saved to data/timing/live-keys.json with
permission 600, never printed and never sent anywhere except Polymarket. Never paste them in a chat.
"""
import getpass
import json
import os
from pathlib import Path


def main():
    folder = Path(os.getenv("PROPHET_DB", "data/timing/prophet.sqlite3")).parent
    folder.mkdir(parents=True, exist_ok=True)
    key_id = getpass.getpass("Polymarket US API key ID (nothing shows while typing): ").strip()
    secret = getpass.getpass("Polymarket US API secret key (nothing shows while typing): ").strip()
    if not key_id or not secret:
        raise SystemExit("Nothing saved: both values are needed.")
    from polymarket_us import PolymarketUS
    client = PolymarketUS(key_id=key_id, secret_key=secret, max_retries=0)
    try:
        balances = client.account.balances().get("balances", [])
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"Nothing saved: Polymarket refused these keys ({type(exc).__name__}). Check them and try again.")
    path = folder / "live-keys.json"
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(dict(key_id=key_id, secret_key=secret), f)
    os.chmod(path, 0o600)
    for b in balances:
        print(f"Keys saved and working. Cash: ${b.get('currentBalance')}  Buying power: ${b.get('buyingPower')}")
    print("No orders were sent. Next step: the test order (scripts.live_test).")


if __name__ == "__main__":
    main()
