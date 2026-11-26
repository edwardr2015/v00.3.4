"""Verified-only history; analysis never submits an order."""

import json
import math
import time


def streaks(values, positive, negative):
    longest = {positive: 0, negative: 0}
    last, run = None, 0
    for value in values:
        if value not in longest:
            if value not in ("SKIP", None):
                last, run = None, 0
            continue
        run = run + 1 if value == last else 1
        last = value
        longest[value] = max(longest[value], run)
    return dict(current=last, length=run, longest=longest)


def initialize(store):
    with store.connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS verified_markets (
          slug TEXT PRIMARY KEY, start REAL NOT NULL, end REAL NOT NULL,
          outcome TEXT NOT NULL CHECK(outcome IN ('UP','DOWN')),
          opening REAL NOT NULL, closing REAL NOT NULL, verified_at REAL NOT NULL,
          source TEXT NOT NULL, evidence TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS trend_runs (
          id INTEGER PRIMARY KEY, time REAL NOT NULL, result TEXT NOT NULL);
        """)


def record_market(store, outcome, start, end):
    if end - start != 300 or outcome.get("yes_payout") not in (0, 1):
        raise ValueError("Invalid five-minute settlement")
    opening, closing = float(outcome["opening"]), float(outcome["closing"])
    if not all(math.isfinite(v) and v > 0 for v in (opening, closing)):
        raise ValueError("Invalid settlement references")
    direction = "UP" if closing >= opening else "DOWN"
    if outcome.get("outcome") != direction or outcome["yes_payout"] != int(direction == "UP"):
        raise ValueError("Settlement and reference disagreement")
    if not outcome.get("source") or not outcome.get("received"):
        raise ValueError("Missing settlement provenance")
    with store.connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO verified_markets VALUES (?,?,?,?,?,?,?,?,?)",
            (
                outcome["slug"],
                start,
                end,
                direction,
                opening,
                closing,
                outcome["received"],
                outcome["source"],
                json.dumps(outcome),
            ),
        )


def history(store):
    with store.connect() as db:
        markets = [
            dict(r) for r in db.execute("SELECT * FROM verified_markets ORDER BY end DESC LIMIT 50")
        ][::-1]
        trades = [
            dict(r)
            for r in db.execute(
                "SELECT * FROM v2_orders WHERE account='primary' AND status='SETTLED' ORDER BY settled,id"
            )
        ]
        skips = [
            dict(r)
            for r in db.execute("SELECT * FROM v2_skips WHERE account='primary' ORDER BY time")
        ]
        round_ends = {
            r["slug"]: json.loads(r["details"]).get("window_end", float("inf"))
            for r in db.execute("SELECT slug,details FROM v2_decisions ORDER BY time")
        }
        open_slugs = {
            r[0]
            for r in db.execute(
                "SELECT slug FROM v2_orders WHERE status='OPEN' AND account='primary'"
            )
        }
    up = sum(m["outcome"] == "UP" for m in markets)
    vals = ["WIN" if t["pnl"] > 0 else "LOSS" if t["pnl"] < 0 else "FLAT" for t in trades]
    tiles = [dict(time=t["settled"], value=v, slug=t["slug"]) for t, v in zip(trades, vals)]
    traded = {t["slug"] for t in trades} | open_slugs
    latest = {
        s["slug"]: s
        for s in skips
        if s["slug"] not in traded and round_ends.get(s["slug"], float("inf")) <= time.time()
    }
    tiles += [dict(time=s["time"], value="SKIP", slug=s["slug"]) for s in latest.values()]
    return dict(
        markets=markets,
        count=len(markets),
        complete=len(markets) == 50,
        up_percent=100 * up / len(markets) if markets else None,
        down_percent=100 * (len(markets) - up) / len(markets) if markets else None,
        directional_streaks=streaks([m["outcome"] for m in markets], "UP", "DOWN"),
        trade_tiles=sorted(tiles, key=lambda t: t["time"])[-50:],
        trade_streaks=streaks(vals, "WIN", "LOSS"),
        total_trades=len(trades),
        win_rate=vals.count("WIN") / len(trades) if trades else None,
    )


def analyze(store, now=None):
    result = history(store)
    markets, n = result["markets"], result["count"]
    result["status"] = "Insufficient Data" if n < 50 else "No Significant Trend"
    result["explanation"] = (
        f"{n}/50 verified settled markets available. Streaks do not guarantee the next outcome."
    )
    if n == 50:
        z = abs(sum(m["outcome"] == "UP" for m in markets) - 25) / math.sqrt(12.5)
        if z >= 1.96:
            result["status"] = "Trend Detected"
        result["explanation"] = (
            f"UP {result['up_percent']:.0f}%, DOWN {result['down_percent']:.0f}%. "
            "Directional imbalance is descriptive and may be chance, especially with repeated testing. Streaks do not predict the next round."
        )
    returns = [math.log(m["closing"] / m["opening"]) for m in markets]
    result["recent_momentum"] = sum(returns[-10:]) if returns else None
    result["volatility"] = (
        (sum((x - sum(returns) / n) ** 2 for x in returns) / n) ** 0.5 if n else None
    )
    result["condition_change"] = (
        sum(returns[-10:]) / 10 - sum(returns[:10]) / 10 if n >= 20 else None
    )
    with store.connect() as db:
        rows = db.execute("""SELECT p.up,m.outcome FROM v2_decisions p JOIN verified_markets m ON m.slug=p.slug
          WHERE p.up IS NOT NULL AND p.time >= m.start AND p.time < m.end AND p.time <= m.verified_at
          AND p.rowid IN (SELECT min(rowid) FROM v2_decisions WHERE up IS NOT NULL GROUP BY slug)
          AND m.slug IN (SELECT slug FROM verified_markets ORDER BY end DESC LIMIT 50)""").fetchall()
        result["prediction_count"] = len(rows)
        result["accuracy"] = (
            sum((r["up"] >= 0.5) == (r["outcome"] == "UP") for r in rows) / len(rows)
            if rows
            else None
        )
        result["brier"] = (
            sum((r["up"] - int(r["outcome"] == "UP")) ** 2 for r in rows) / len(rows)
            if len(rows) >= 20
            else None
        )
        result["calibration_bins"] = None
        result["calibration_error"] = None
        if len(rows) >= 20:
            bins = []
            for b in range(5):
                selected = [
                    r for r in rows if b / 5 <= r["up"] < ((b + 1) / 5 if b < 4 else 1.00001)
                ]
                bins.append(
                    dict(
                        lower=b / 5,
                        count=len(selected),
                        mean_probability=sum(r["up"] for r in selected) / len(selected)
                        if selected
                        else None,
                        observed_up=sum(r["outcome"] == "UP" for r in selected) / len(selected)
                        if selected
                        else None,
                    )
                )
            result["calibration_bins"] = bins
            result["calibration_error"] = sum(
                b["count"] * abs(b["mean_probability"] - b["observed_up"])
                for b in bins
                if b["count"]
            ) / len(rows)
        result["calibration_status"] = (
            "AVAILABLE" if len(rows) >= 20 else "INSUFFICIENT DATA (20 predictions required)"
        )
        db.execute(
            "INSERT INTO trend_runs(time,result) VALUES (?,?)",
            (time.time() if now is None else now, json.dumps(result)),
        )
    return result
