"""Price feed check (dev14): is the bot's reference price the same one Polymarket settles on?

Once per round per minute the bot's reference and opening price are logged. The report then
compares them with the official opening and closing prices in verified_markets. A large or
lagging difference would explain why the model has been overconfident.
"""


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS feed_checks (
            slug TEXT NOT NULL, minute INTEGER NOT NULL, time REAL NOT NULL, elapsed REAL,
            reference_price REAL, opening_price REAL, reference_timestamp REAL,
            PRIMARY KEY(slug, minute));""")


def record(db, slug, now, start, details):
    ref, opening = details.get("reference_price"), details.get("opening_price")
    if not ref or not opening:
        return
    minute = min(4, max(0, int((now - start) // 60)))
    db.execute("INSERT OR IGNORE INTO feed_checks VALUES (?,?,?,?,?,?,?)",
               (slug, minute, now, now - start, ref, opening, details.get("reference_timestamp")))


def rows(db, limit=500):
    """One row per settled round: bot opening vs official opening, and whether the bot's
    last reading pointed the same way as the official result."""
    out = []
    for r in db.execute("""SELECT v.slug, v.start, v.opening AS official_open, v.closing AS official_close,
            v.outcome, f0.opening_price AS bot_open, f4.reference_price AS bot_last, f4.elapsed AS last_elapsed,
            f4.time - f4.reference_timestamp AS feed_age
        FROM verified_markets v
        JOIN feed_checks f0 ON f0.slug=v.slug AND f0.minute=(SELECT min(minute) FROM feed_checks WHERE slug=v.slug)
        JOIN feed_checks f4 ON f4.slug=v.slug AND f4.minute=(SELECT max(minute) FROM feed_checks WHERE slug=v.slug)
        ORDER BY v.start DESC LIMIT ?""", (limit,)):
        r = dict(r)
        r["open_difference"] = r["bot_open"] - r["official_open"]
        last_up = r["bot_last"] > r["bot_open"]
        r["bot_direction_matches"] = (("UP" if last_up else "DOWN") == r["outcome"]) if r["bot_last"] != r["bot_open"] else None
        out.append(r)
    return out


def summary(db):
    data = rows(db, 10000)
    if not data:
        return dict(rounds=0)
    diffs = sorted(abs(r["open_difference"]) for r in data)
    late = [r for r in data if r["last_elapsed"] and r["last_elapsed"] >= 240 and r["bot_direction_matches"] is not None]
    ages = sorted(r["feed_age"] for r in data if r["feed_age"] is not None)
    return dict(rounds=len(data), median_open_gap=diffs[len(diffs) // 2], worst_open_gap=diffs[-1],
                minute4_direction_right=(sum(r["bot_direction_matches"] for r in late) / len(late)) if late else None,
                minute4_rounds=len(late), median_feed_age=ages[len(ages) // 2] if ages else None)
