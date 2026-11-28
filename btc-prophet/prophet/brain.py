"""2.2 brain: the bot's full loop - look, decide, check, learn - in one place.

LOOK    Once per minute of every round it records what every voice says the chance of UP is:
        the market price, the main model, the official-rule model (delayed feed and live feed).
CHECK   When the round settles it grades every voice against the real result (Brier score:
        0 is perfect, 0.25 is a coin flip).
LEARN   For each minute it learns how much to trust the models over the market:
        blended = market + trust x (model - market), trust picked from 0..1 by the best score on
        recent settled rounds. Trust near 0 means "the market knows best right now".
DECIDE  It ranks every account by real results (with a 95% range) and says, in plain words,
        which one the live trader should follow. It does not switch real money by itself.
"""
import json
import math
import time

VOICES = ("market", "main", "settlement", "settlement_live")
TRUST_GRID = tuple(i / 10 for i in range(11))


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS brain_thoughts (
            slug TEXT NOT NULL, minute INTEGER NOT NULL, time REAL NOT NULL, opinions TEXT NOT NULL,
            PRIMARY KEY(slug, minute));""")


def record(db, slug, now, start, opinions):
    """LOOK: one row per round per minute (the first observation of that minute)."""
    clean = {k: float(v) for k, v in opinions.items() if v is not None and 0 <= v <= 1}
    if "market" not in clean:
        return
    db.execute("INSERT OR IGNORE INTO brain_thoughts VALUES (?,?,?,?)",
               (slug, min(4, max(0, int((now - start) // 60))), now, json.dumps(clean)))


def _graded(db, limit=3000):
    rows = db.execute("""SELECT t.minute, t.opinions, v.outcome FROM brain_thoughts t
        JOIN verified_markets v ON v.slug=t.slug ORDER BY t.time DESC LIMIT ?""", (limit,)).fetchall()
    return [(r["minute"], json.loads(r["opinions"]), 1.0 if r["outcome"] == "UP" else 0.0) for r in rows]


def scores(db):
    """CHECK + LEARN: per minute, each voice's Brier score and the learned trust in each model."""
    graded = _graded(db)
    out = {}
    for minute in range(5):
        rows = [(o, y) for m, o, y in graded if m == minute]
        if not rows:
            continue
        entry = dict(rounds=len(rows), brier={}, trust={})
        for v in VOICES:
            pts = [(o[v], y) for o, y in rows if v in o]
            if len(pts) >= 10:
                entry["brier"][v] = sum((p - y) ** 2 for p, y in pts) / len(pts)
        for v in VOICES[1:]:
            pts = [(o["market"], o[v], y) for o, y in rows if v in o]
            if len(pts) >= 20:
                best = min(TRUST_GRID, key=lambda w: sum((m + w * (p - m) - y) ** 2 for m, p, y in pts))
                entry["trust"][v] = best
        out[minute] = entry
    return out


def _range(returns):
    n = len(returns)
    if n < 2:
        return None, None
    mean = sum(returns) / n
    sd = math.sqrt(sum((r - mean) ** 2 for r in returns) / (n - 1))
    return mean - 1.96 * sd / math.sqrt(n), mean + 1.96 * sd / math.sqrt(n)


def leaderboard(db):
    """DECIDE: every account ranked by real results per $1 risked."""
    from . import audit
    sources = [("Main $1,000", "unified_trades", "cost", "pnl", None),
               ("Selective $50", "selective50_trades", "cost", "pnl", None),
               ("Selective $1,000 at $50", "selective1000_trades", "cost", "pnl", None),
               ("1-hour fade", "hourly_trades", "cost", "pnl", None)]
    if audit._table(db, "shadow_rule_trades"):
        sources += [(f"Test: {r}", "shadow_rule_trades", "cost", "pnl", r)
                    for (r,) in db.execute("SELECT DISTINCT rule FROM shadow_rule_trades")]
    if audit._table(db, "fresh_trades"):
        sources += [(f"Test: {r}", "fresh_trades", "quantity*price+fee", "pnl", r)
                    for (r,) in db.execute("SELECT DISTINCT rule FROM fresh_trades")]
    board = []
    for name, table, cost, pnl, rule in sources:
        if not audit._table(db, table):
            continue
        flt = f" AND rule='{rule}'" if rule else ""
        rows = db.execute(f"SELECT {pnl} AS p, {cost} AS c FROM {table} WHERE {pnl} IS NOT NULL{flt}").fetchall()
        rets = [r["p"] / r["c"] for r in rows if r["c"]]
        if not rets:
            continue
        low, high = _range(rets)
        board.append(dict(name=name, trades=len(rets), per_dollar=sum(rets) / len(rets), low=low, high=high,
                          net=sum(r["p"] for r in rows), wins=sum(r["p"] > 0 for r in rows)))
    board.sort(key=lambda b: (b["low"] if b["low"] is not None else -9, b["per_dollar"]), reverse=True)
    return board


def thinking(db):
    """Plain-English summary of what the bot has learned."""
    import sqlite3
    try:
        initialize(db)
    except sqlite3.OperationalError:         # read-only report on an older database
        pass
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='brain_thoughts'").fetchone():
        return dict(time=time.time(), summary=["Brain not started yet: it begins with the first observed round."],
                    minutes={}, leaderboard=leaderboard(db))
    sc = scores(db)
    board = leaderboard(db)
    lines = []
    for minute, e in sorted(sc.items()):
        b = e["brier"]
        if "market" in b and len(b) > 1:
            best = min(b, key=b.get)
            trust = e["trust"].get("settlement_live", e["trust"].get("settlement", e["trust"].get("main")))
            lines.append(f"Minute {minute}: most accurate = {best.replace('_', ' ')} (score {b[best]:.3f}, market {b['market']:.3f})"
                         + (f"; trusts the models {int(100 * trust)}% vs the market" if trust is not None else "")
                         + f" · {e['rounds']} rounds")
    if board:
        top = board[0]
        proven = top["trades"] >= 30 and top["low"] is not None and top["low"] > 0
        lines.append(f"Best account so far: {top['name']} - {top['trades']} trades, {top['wins']} wins, "
                     f"{100 * top['per_dollar']:+.1f}¢ per $1" + (" (profit range above zero)" if proven else
                     " (not yet proven: its range still includes losing)"))
        live = next((b for b in board if b["name"] == "Selective $50"), None)
        if live and top["name"] != "Selective $50":
            lines.append(f"The live trader follows Selective $50 ({100 * live['per_dollar']:+.1f}¢ per $1); "
                         f"{top['name']} is doing better so far - worth reviewing.")
    if not lines:
        lines.append("Still collecting: the brain needs settled rounds before it can grade anything.")
    return dict(time=time.time(), summary=lines, minutes=sc, leaderboard=board)
