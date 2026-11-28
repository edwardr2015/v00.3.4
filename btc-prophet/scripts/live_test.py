"""Check how Polymarket reads DOWN orders, then (only with --buy) make two tiny real test trades.

    .venv/bin/python -m scripts.live_test          # preview only: sends NO orders
    .venv/bin/python -m scripts.live_test --buy    # about $1 total: buys 1 UP and 1 DOWN share, then sells them

Prints the exchange's answers (no keys). Paste the output in the chat so the DOWN price convention
can be confirmed before any $3 trade.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def slug_now():
    start = int(time.time()) // 300 * 300
    return datetime.fromtimestamp(start, timezone.utc).strftime("cpc-btc-updown-5m-%Y-%m-%d-%H%Mz"), start


def amount(x):
    return dict(value=f"{x:.2f}", currency="USD")


def main():
    folder = Path(os.getenv("PROPHET_DB", "data/timing/prophet.sqlite3")).parent
    keys = json.loads((folder / "live-keys.json").read_text())
    from polymarket_us import PolymarketUS
    client = PolymarketUS(key_id=keys["key_id"], secret_key=keys["secret_key"], max_retries=0)
    slug, start = slug_now()
    if time.time() - start > 200:
        print("Less than 100 seconds left in this round; wait for the next one and run again.")
        return 1
    bbo = client.markets.bbo(slug)
    print("Market:", slug)
    print("Best bid/offer (UP side):", json.dumps(bbo)[:400])
    def px(v):
        return float(v["value"] if isinstance(v, dict) else v)
    md = bbo.get("marketData", bbo)
    bid, ask = px(md.get("bestBid") or md["bid"]), px(md.get("bestAsk") or md["ask"])
    down_ask = round(1 - bid, 2)
    print(f"UP ask {ask:.2f}  ->  DOWN ask {down_ask:.2f}")
    base = dict(marketSlug=slug, type="ORDER_TYPE_LIMIT", quantity=1, tif="TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
                manualOrderIndicator="MANUAL_ORDER_INDICATOR_MANUAL", synchronousExecution=True)
    tests = [("UP buy at UP price", dict(base, intent="ORDER_INTENT_BUY_LONG", price=amount(ask))),
             ("DOWN buy, price written as DOWN price", dict(base, intent="ORDER_INTENT_BUY_SHORT", price=amount(down_ask))),
             ("DOWN buy, price written as UP price", dict(base, intent="ORDER_INTENT_BUY_SHORT", price=amount(1 - down_ask)))]
    for name, req in tests:
        try:
            out = client.orders.preview(dict(request=req))
            o = out.get("order", {})
            print(f"PREVIEW {name}: state={o.get('state')} price={o.get('price')} avgPx={o.get('avgPx')} "
                  f"cash={o.get('cashOrderQty')} fee={o.get('commissionNotionalTotalCollected')}")
        except Exception as exc:  # noqa: BLE001
            print(f"PREVIEW {name}: REFUSED {type(exc).__name__}: {str(exc)[:200]}")
    if "--buy" not in sys.argv:
        print("No orders were sent. Paste everything above into the chat.")
        return 0
    convention = os.getenv("SHORT_PRICE", "")
    if convention not in ("down", "up"):
        print("Set SHORT_PRICE=down or SHORT_PRICE=up (Claude tells you which) before using --buy.")
        return 1
    down_price = down_ask if convention == "down" else round(1 - down_ask, 2)
    for name, intent, price in (("UP", "ORDER_INTENT_BUY_LONG", ask), ("DOWN", "ORDER_INTENT_BUY_SHORT", down_price)):
        out = client.orders.create(dict(base, intent=intent, price=amount(price)))     # sent once
        print(f"BUY {name}:", json.dumps(out)[:600])
    time.sleep(2)
    print("Positions:", json.dumps(client.portfolio.positions())[:1500])
    for name, intent in (("UP", "ORDER_INTENT_SELL_LONG"), ("DOWN", "ORDER_INTENT_SELL_SHORT")):
        price = .01 if (name == "UP" or convention == "down") else .99   # sell at the best available, never worse than 1c
        out = client.orders.create(dict(base, intent=intent, price=amount(price)))
        print(f"SELL {name}:", json.dumps(out)[:600])
    (folder / "live-convention.json").write_text(json.dumps(dict(short_price=convention, tested=time.time())))
    print("Test finished. Paste everything above into the chat.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
