"""Hammer the real primary paper ledger (prophet.unified) with recorded quotes plus injected chaos.

Uses a throwaway SQLite database in a temp folder; never touches a real data directory.
After every market it checks accounting invariants:
  I1 cash = 1000 + realised P&L - cost of open positions      I4 cash never negative
  I2 at most one open position                                 I5 settlement pays qty or 0
  I3 at most one trade per market                              I6 entry inside the market window
Runs: clean, clean again (determinism), duplicated ticks, shuffled ticks, stale replays,
restart mid-run, double settlement, 20% of markets with no feed.

    python research/ledger_stress.py --quotes-cache quotes.pkl --out ledger.json
"""

import argparse
import json
import random
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "btc-prophet"))
from prophet import unified  # noqa: E402
from prophet.app import create_app  # noqa: E402
from prophet.store import Store  # noqa: E402
from quotes import load  # noqa: E402

MODEL = "volatility-reference-gap-v1"


def fresh(folder):
    store = Store(str(Path(folder) / "prophet.sqlite3"))
    create_app(store.path, collect=False, auth_required=False).state.paper.recover_startup(1000)
    return store


def observe(db, q):
    end = q["t"] + q["left"]
    start = end - 300
    details = dict(window_start=start, window_end=end, book_checked=q["t"], reference_timestamp=q["t"])
    book = dict(bids=[dict(price=q["bid"], quantity=q["bid_q"])], asks=[dict(price=q["ask"], quantity=q["ask_q"])])
    unified.observe(db, q["slug"], q["t"], "SIMULATE", MODEL, q["gap_p"], None, details, book, q["terms"],
                    lambda: q["t"])


def verify(db, slug, start, end, outcome):
    db.execute("INSERT OR IGNORE INTO verified_markets VALUES (?,?,?,?,?,?,?,?,?)",
               (slug, start, end, outcome, 1, 1, end + 20, "stress-fixture", "{}"))


def check(db, violations, slug):
    cash = db.execute("SELECT balance FROM unified_account WHERE id=1").fetchone()[0]
    rows = db.execute("SELECT t.*, r.start, r.end FROM unified_trades t JOIN unified_rounds r USING(slug)").fetchall()
    realised = sum(r["pnl"] for r in rows if r["status"] != "OPEN")
    open_cost = sum(r["cost"] for r in rows if r["status"] == "OPEN")
    def bad(code, detail):
        violations.append(dict(after=slug, invariant=code, detail=detail))
    if abs(cash - (1000 + realised - open_cost)) > 1e-6:
        bad("I1", dict(cash=cash, expected=1000 + realised - open_cost))
    if sum(r["status"] == "OPEN" for r in rows) > 1:
        bad("I2", "more than one open position")
    if len({r["slug"] for r in rows}) != len(rows):
        bad("I3", "duplicate trade for a market")
    if cash < -1e-9:
        bad("I4", cash)
    for r in rows:
        if not r["start"] <= r["time"] < r["end"]:
            bad("I6", dict(slug=r["slug"], time=r["time"]))
        if r["status"] == "SETTLED":
            o = db.execute("SELECT outcome FROM verified_markets WHERE slug=?", (r["slug"],)).fetchone()[0]
            if abs(r["proceeds"] - (r["quantity"] if r["side"] == o else 0)) > 1e-9:
                bad("I5", dict(slug=r["slug"], proceeds=r["proceeds"]))


def run(data, mode, seed=5):
    rng = random.Random(seed)
    by = {}
    for q in data["quotes"]:
        by.setdefault(q["slug"], []).append(q)
    order = sorted(by, key=lambda s: by[s][0]["t"])
    violations, ticks, began = [], 0, time.time()
    with tempfile.TemporaryDirectory() as folder:
        store = fresh(folder)
        seen = []
        for k, slug in enumerate(order):
            quotes = list(by[slug])
            if mode == "outage" and rng.random() < .2:
                quotes = []
            if mode == "duplicate":
                quotes = [q for q in quotes for _ in (0, 1)]
            if mode == "shuffle":
                rng.shuffle(quotes)
            if mode == "stale" and seen and quotes:
                # A stale book served as fresh: old prices, current market window and timestamps.
                stale_rows = []
                for q in quotes:
                    if rng.random() < .3:
                        old = rng.choice(seen)
                        stale_rows.append(dict(q, bid=old["bid"], ask=old["ask"], bid_q=old["bid_q"],
                                               ask_q=old["ask_q"]))
                    else:
                        stale_rows.append(q)
                quotes = stale_rows
            if mode == "restart" and k == len(order) // 2:
                store = Store(store.path)
                create_app(store.path, collect=False, auth_required=False).state.paper.recover_startup(1000)
            with store.connect() as db:
                for q in quotes:
                    try:
                        observe(db, q)
                    except Exception as exc:  # a crash is a finding, not a skip
                        violations.append(dict(after=slug, invariant="EXCEPTION", detail=repr(exc)[:200]))
                    ticks += 1
                end = by[slug][0]["t"] + by[slug][0]["left"]
                verify(db, slug, end - 300, end, "UP" if by[slug][0]["up"] else "DOWN")
                unified.settle(db, end + 21)
                if mode == "double_settle":
                    unified.settle(db, end + 22)
                    unified.settle(db, end + 500)
                check(db, violations, slug)
            seen.extend(by[slug][:3])
        with store.connect() as db:
            trades = [dict(r) for r in db.execute("SELECT slug,side,quantity,cost,status,pnl FROM unified_trades ORDER BY time")]
            cash = db.execute("SELECT balance FROM unified_account WHERE id=1").fetchone()[0]
    settled = [t for t in trades if t["pnl"] is not None]
    return dict(mode=mode, markets=len(order), ticks=ticks, seconds=round(time.time() - began, 1),
                ticks_per_second=round(ticks / max(time.time() - began, 1e-9)), trades=len(trades),
                wins=sum(t["pnl"] > 0 for t in settled), pnl=round(sum(t["pnl"] for t in settled), 2),
                final_cash=round(cash, 4), early_exits=sum(t["status"] == "SOLD" for t in trades),
                violations=len(violations), first_violations=violations[:5],
                fingerprint=json.dumps([(t["slug"], t["side"], round(t["cost"], 6), t["status"]) for t in trades]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quotes-cache", required=True)
    ap.add_argument("--out")
    a = ap.parse_args()
    data = load(None, a.quotes_cache)
    results = [run(data, m) for m in ("clean", "clean", "duplicate", "shuffle", "stale", "restart",
                                      "double_settle", "outage")]
    results[1]["mode"] = "clean (repeat)"
    results[1]["identical_to_first_run"] = results[1]["fingerprint"] == results[0]["fingerprint"]
    reference = results[0]["fingerprint"]
    for r in results:
        r["same_trades_as_clean"] = r["fingerprint"] == reference
    for r in results:
        del r["fingerprint"]
    text = json.dumps(results, indent=2)
    print(text)
    if a.out:
        Path(a.out).write_text(text)


if __name__ == "__main__":
    main()
