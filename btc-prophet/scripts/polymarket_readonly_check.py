"""READ-ONLY Polymarket US connection check. It never places, previews or cancels an order.

    .venv/bin/python -m scripts.polymarket_readonly_check          # public prices only, no keys
    POLYMARKET_KEY_ID=... POLYMARKET_SECRET_KEY=... .venv/bin/python -m scripts.polymarket_readonly_check

Step 1 reads the live price of the current 5-minute BTC market (no keys needed).
Step 2, only when both key variables are set, reads your account balance and open positions.
Keys are read from the environment, never saved and never printed.
"""
import os
import sys
import time
from datetime import datetime, timezone


def slug_for(ts):
    start = int(ts) // 300 * 300
    return datetime.fromtimestamp(start, timezone.utc).strftime("cpc-btc-updown-5m-%Y-%m-%d-%H%Mz")


def main():
    try:
        from polymarket_us import PolymarketUS
    except ImportError:
        print("Missing SDK. Run:  .venv/bin/python -m pip install polymarket-us")
        return 1
    key, secret = os.environ.get("POLYMARKET_KEY_ID"), os.environ.get("POLYMARKET_SECRET_KEY")
    client = PolymarketUS(key_id=key, secret_key=secret, max_retries=0)
    ok = True
    print("Step 1: public market data (no keys)")
    for ts in (time.time(), time.time() - 300):
        slug = slug_for(ts)
        try:
            print("  PASS", slug, "best bid/offer:", client.markets.bbo(slug))
            break
        except Exception as exc:  # noqa: BLE001
            print("  could not read", slug, "->", type(exc).__name__, str(exc)[:200])
    else:
        ok = False
    if not (key and secret):
        print("Step 2 skipped: set POLYMARKET_KEY_ID and POLYMARKET_SECRET_KEY to test your login.")
        return 0 if ok else 1
    print("Step 2: your account (read only)")
    for name, call in (("balance", client.account.balances), ("positions", client.portfolio.positions)):
        try:
            print("  PASS", name + ":", call())
        except Exception as exc:  # noqa: BLE001
            ok = False
            print("  FAIL", name, "->", type(exc).__name__, str(exc)[:200])
    print("No orders were sent. Real-money trading in BTC Prophet stays OFF.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
