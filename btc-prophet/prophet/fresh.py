"""dev16: fresh data, the official settlement rule, and a limit-order paper test.

Four parts, all paper only (nothing here can place an order or touch the main account unless the
owner turns on the main-model switch, which only changes the main account's forecast number):

1. Recorder      Every observation (about once a second) the order book (top 5 levels), the BRTI
                 reference and the Kraken/Coinbase spot price are saved, so later research can use
                 second-by-second data instead of one-minute history.
2. Lag check     For every settled round the bot's own saved BRTI seconds are averaged exactly as the
                 official rule says (60 values before the boundary) and compared with Polymarket's
                 official opening/closing values, at time shifts of -60..+60 s. If shift 0 matches,
                 the feed is live; if another shift matches, the feed is that many seconds late.
3. Settlement    The official rule (settlement_model.probability) with volatility scale learned from
   model         the bot's own settled rounds (best Brier score on a grid), refit hourly.
4. Limit orders  "DOWN near 55c" test: when UP trades 40-50c, rest a virtual DOWN bid at the current
                 DOWN bid. It only counts as filled when the market later trades THROUGH that price
                 (DOWN ask at least one tick below it). Holds to the end. P&L is shown both with the
                 taker fee charged (conservative) and without it.
"""
import json
import math
import time

from . import settlement_model

RECORD_LEVELS = 5
STARTING_CASH = 50.0
STAKE = 3.0
SCALES = (1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 3.0)
DEFAULT_SCALE = 2.0
MIN_FIT_ROUNDS = 50
SETTLE_EDGE = .03


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS fresh_recorder (
            time REAL NOT NULL, slug TEXT NOT NULL, elapsed REAL, book TEXT NOT NULL,
            book_timestamp REAL, reference_price REAL, reference_timestamp REAL,
            spot_price REAL, spot_timestamp REAL, spot_source TEXT);
        CREATE INDEX IF NOT EXISTS fresh_recorder_time ON fresh_recorder(time);
        CREATE TABLE IF NOT EXISTS fresh_forecasts (
            slug TEXT NOT NULL, bucket INTEGER NOT NULL, time REAL NOT NULL, seconds_left REAL NOT NULL,
            reference REAL NOT NULL, opening REAL NOT NULL, vol REAL NOT NULL, fixed_average REAL,
            market_up REAL, PRIMARY KEY(slug, bucket));
        CREATE TABLE IF NOT EXISTS fresh_lag_checks (
            slug TEXT PRIMARY KEY, end REAL NOT NULL, official_open REAL, official_close REAL,
            our_open REAL, our_close REAL, best_shift INTEGER, best_error REAL, zero_error REAL,
            seconds INTEGER);
        CREATE TABLE IF NOT EXISTS fresh_accounts (
            rule TEXT PRIMARY KEY, started REAL NOT NULL, balance REAL NOT NULL CHECK(balance>=0));
        CREATE TABLE IF NOT EXISTS fresh_trades (
            rule TEXT NOT NULL, slug TEXT NOT NULL, placed REAL NOT NULL, filled REAL, end REAL NOT NULL,
            side TEXT NOT NULL, price REAL NOT NULL, quantity REAL NOT NULL, fee REAL NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('RESTING','FILLED','CANCELLED','SETTLED')),
            outcome TEXT, pnl REAL, pnl_no_fee REAL, details TEXT NOT NULL, PRIMARY KEY(rule, slug));""")


# ---------------------------------------------------------------- helpers
def _brti(db, start, stop):
    """BRTI one-second closes with start <= time < stop, as {time: close}."""
    return {r[0]: r[1] for r in db.execute(
        "SELECT time, close FROM candles WHERE source='BRTI' AND time>=? AND time<? ORDER BY time",
        (start, stop))}


def _average(values, start, stop):
    got = [values[t] for t in range(int(start), int(stop)) if t in values]
    return (sum(got) / len(got), len(got)) if got else (None, 0)


def _fee(theta, price):
    return theta * price * (1 - price)


def scale(db):
    """Volatility scale learned from settled rounds (best Brier on the grid), refit at most hourly."""
    row = db.execute("SELECT payload FROM state WHERE key='fresh_vol_scale'").fetchone()
    cached = json.loads(row[0]) if row else None
    now = time.time()
    if cached and now - cached.get("fitted", 0) < 3600:
        return cached["scale"]
    rows = db.execute("""SELECT f.*, v.outcome FROM fresh_forecasts f JOIN verified_markets v ON v.slug=f.slug
        ORDER BY f.time DESC LIMIT 4000""").fetchall()
    rounds = len({r["slug"] for r in rows})
    best, value = DEFAULT_SCALE, None
    if rounds >= MIN_FIT_ROUNDS:
        scores = {}
        for s in SCALES:
            err = []
            for r in rows:
                fixed = [r["fixed_average"]] if r["fixed_average"] else ()
                p = settlement_model.probability(r["reference"], r["opening"], r["vol"], r["seconds_left"],
                                                 fixed, vol_scale=s)
                if p is not None:
                    err.append((p - (r["outcome"] == "UP")) ** 2)
            if err:
                scores[s] = sum(err) / len(err)
        if scores:
            best = min(scores, key=scores.get)
            value = scores[best]
    db.execute("INSERT OR REPLACE INTO state VALUES ('fresh_vol_scale',?,?)",
               (json.dumps(dict(scale=best, fitted=now, rounds=rounds, brier=value)), now))
    return best


def settlement_up(db, now, start, end, opening, reference, vol):
    """Official-rule probability of UP right now, or None."""
    if not (opening and reference and vol):
        return None, None
    left = end - now
    fixed = None
    if left <= settlement_model.WINDOW:
        fixed, _ = _average(_brti(db, end - settlement_model.WINDOW, now + 1), end - settlement_model.WINDOW, now + 1)
    s = scale(db)
    p = settlement_model.probability(reference, opening, vol, left, [fixed] if fixed else (), vol_scale=s)
    return p, dict(scale=s, seconds_left=left, fixed_average=fixed)


# ---------------------------------------------------------------- observation hook
def observe(db, slug, now, details, book, terms, mode):
    start, end = details["window_start"], details["window_end"]
    spot = db.execute("SELECT payload FROM state WHERE key='spot'").fetchone()
    spot = json.loads(spot[0]) if spot else {}
    top = dict(bids=book.get("bids", [])[:RECORD_LEVELS], asks=book.get("asks", [])[:RECORD_LEVELS])
    db.execute("INSERT INTO fresh_recorder VALUES (?,?,?,?,?,?,?,?,?,?)",
               (now, slug, now - start, json.dumps(top), details.get("book_timestamp"), details.get("reference_price"),
                details.get("reference_timestamp"), spot.get("price"), spot.get("timestamp"), spot.get("source")))
    if not book.get("bids") or not book.get("asks"):
        return None
    mid = (book["bids"][0]["price"] + book["asks"][0]["price"]) / 2
    p, info = settlement_up(db, now, start, end, details.get("opening_price"), details.get("reference_price"),
                            details.get("volatility"))
    if p is not None:
        db.execute("INSERT OR IGNORE INTO fresh_forecasts VALUES (?,?,?,?,?,?,?,?,?)",
                   (slug, int((now - start) // 30), now, end - now, details["reference_price"],
                    details["opening_price"], details["volatility"], info["fixed_average"], mid))
    live_p = None
    try:
        from . import feeds
        live = feeds.live_brti(db, now)
    except Exception:  # noqa: BLE001 - no feed tables yet
        live = None
    if live:
        live_p, live_info = settlement_up(db, now, start, end, details.get("opening_price"), live, details.get("volatility"))
        if live_info:
            live_info["live_reference"] = live
    if mode == "SIMULATE":
        _settlement_account(db, slug, now, start, end, p, info, book, terms)
        if live_p is not None:
            _settlement_account(db, slug, now, start, end, live_p, live_info, book, terms, rule="settlement_live")
        _limit_account(db, slug, now, start, end, book, terms)
    return dict(standard=p, live=live_p)


def _balance(db, rule, now):
    row = db.execute("SELECT balance FROM fresh_accounts WHERE rule=?", (rule,)).fetchone()
    if row is None:
        db.execute("INSERT INTO fresh_accounts VALUES (?,?,?)", (rule, now, STARTING_CASH))
        return STARTING_CASH
    return row[0]


def _settlement_account(db, slug, now, start, end, p, info, book, terms, rule="settlement_model"):
    """Taker account using the official-rule forecast: 3% edge after fees, 30-70c, hold to the end."""
    from .execution import paper_buy
    if p is None or db.execute("SELECT 1 FROM fresh_trades WHERE rule=? AND slug=?", (rule, slug)).fetchone():
        return
    if not 30 <= now - start <= 270:
        return
    balance = _balance(db, rule, now)
    if balance < STAKE:
        return
    best = None
    for side, q in (("UP", p), ("DOWN", 1 - p)):
        fill, _ = paper_buy(book, side, STAKE, **terms)
        if not fill or fill["total"] > balance or not .30 <= fill["average_price"] <= .70:
            continue
        ev = q * fill["quantity"] - fill["total"]
        if ev >= SETTLE_EDGE * fill["total"] and (best is None or ev > best[2]):
            best = (side, fill, ev, q)
    if not best:
        return
    side, fill, ev, q = best
    db.execute("""INSERT INTO fresh_trades VALUES (?,?,?,?,?,?,?,?,?,'FILLED',NULL,NULL,NULL,?)""",
               (rule, slug, now, now, end, side, fill["average_price"], fill["quantity"], fill["fee"],
                json.dumps(dict(probability=q, expected_value=ev, total=fill["total"], **info))))
    db.execute("UPDATE fresh_accounts SET balance=round(balance-?,10) WHERE rule=?", (fill["total"], rule))


def _limit_account(db, slug, now, start, end, book, terms):
    """Resting DOWN bid when UP is 40-50c. Filled only if the market later trades through it."""
    rule = "limit_down_55"
    theta, tick = terms.get("theta", .11), terms.get("tick", .01)
    up_bid, up_ask = book["bids"][0]["price"], book["asks"][0]["price"]
    down_bid, down_ask = round(1 - up_ask, 4), round(1 - up_bid, 4)
    order = db.execute("SELECT * FROM fresh_trades WHERE rule=? AND slug=?", (rule, slug)).fetchone()
    elapsed = now - start
    if order is None:
        mid = (up_bid + up_ask) / 2
        if not (45 <= elapsed <= 200 and .40 <= mid <= .50 and .45 <= down_bid <= .62):
            return
        balance = _balance(db, rule, now)
        reserve = STAKE + 0.05
        if balance < reserve:
            return
        quantity = math.floor(STAKE / down_bid * 100) / 100
        fee = round(_fee(theta, down_bid) * quantity, 4)
        if quantity <= 0 or quantity * down_bid + fee > balance:
            return
        db.execute("""INSERT INTO fresh_trades VALUES (?,?,?,NULL,?,'DOWN',?,?,?,'RESTING',NULL,NULL,NULL,?)""",
                   (rule, slug, now, end, down_bid, quantity, fee,
                    json.dumps(dict(up_mid=mid, down_bid=down_bid, down_ask=down_ask, elapsed=elapsed))))
        db.execute("UPDATE fresh_accounts SET balance=round(balance-?,10) WHERE rule=?",
                   (quantity * down_bid + fee, rule))   # money is reserved while the order rests
        return
    if order["status"] != "RESTING":
        return
    if down_ask <= order["price"] - tick + 1e-9:          # someone sold DOWN below our price: we were hit
        db.execute("UPDATE fresh_trades SET status='FILLED', filled=? WHERE rule=? AND slug=?", (now, rule, slug))
    elif elapsed >= 240:                                   # too late: cancel and release the money
        db.execute("UPDATE fresh_trades SET status='CANCELLED' WHERE rule=? AND slug=?", (rule, slug))
        db.execute("UPDATE fresh_accounts SET balance=round(balance+?,10) WHERE rule=?",
                   (order["quantity"] * order["price"] + order["fee"], rule))


# ---------------------------------------------------------------- settlement and lag check
def settle(db, now):
    for t in db.execute("""SELECT t.*, v.outcome AS verified FROM fresh_trades t JOIN verified_markets v
            ON v.slug=t.slug AND v.end=t.end WHERE t.status IN ('FILLED','RESTING') AND v.verified_at<=?""",
                        (now,)).fetchall():
        if t["status"] == "RESTING":                       # never filled: release the reserved money
            db.execute("UPDATE fresh_trades SET status='CANCELLED', outcome=? WHERE rule=? AND slug=?",
                       (t["verified"], t["rule"], t["slug"]))
            db.execute("UPDATE fresh_accounts SET balance=round(balance+?,10) WHERE rule=?",
                       (t["quantity"] * t["price"] + t["fee"], t["rule"]))
            continue
        payout = t["quantity"] if t["side"] == t["verified"] else 0.0
        spent = t["quantity"] * t["price"]
        pnl, pnl_no_fee = round(payout - spent - t["fee"], 6), round(payout - spent, 6)
        done = db.execute("""UPDATE fresh_trades SET status='SETTLED', outcome=?, pnl=?, pnl_no_fee=?
            WHERE rule=? AND slug=? AND status='FILLED'""", (t["verified"], pnl, pnl_no_fee, t["rule"], t["slug"]))
        if done.rowcount:
            db.execute("UPDATE fresh_accounts SET balance=round(balance+?,10) WHERE rule=?", (payout, t["rule"]))
    _lag_checks(db)


def _lag_checks(db, limit=20):
    """Compare our saved BRTI seconds with the official opening/closing values."""
    for m in db.execute("""SELECT v.slug, v.start, v.end, v.opening, v.closing FROM verified_markets v
            LEFT JOIN fresh_lag_checks c ON c.slug=v.slug WHERE c.slug IS NULL ORDER BY v.end DESC LIMIT ?""",
                        (limit,)).fetchall():
        values = _brti(db, m["start"] - 130, m["end"] + 70)
        if len(values) < 300:
            db.execute("INSERT OR IGNORE INTO fresh_lag_checks(slug,end,seconds) VALUES (?,?,?)",
                       (m["slug"], m["end"], len(values)))
            continue
        errors = {}
        for shift in range(-60, 61):
            o, no = _average(values, m["start"] - 60 + shift, m["start"] + shift)
            c, nc = _average(values, m["end"] - 60 + shift, m["end"] + shift)
            if no >= 55 and nc >= 55:
                errors[shift] = (abs(o - m["opening"]) + abs(c - m["closing"])) / 2
        if not errors:
            continue
        best = min(errors, key=errors.get)
        o0, _ = _average(values, m["start"] - 60, m["start"])
        c0, _ = _average(values, m["end"] - 60, m["end"])
        db.execute("INSERT OR REPLACE INTO fresh_lag_checks VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (m["slug"], m["end"], m["opening"], m["closing"], o0, c0, best, errors[best],
                    errors.get(0), len(values)))


# ---------------------------------------------------------------- summaries
def lag_summary(db):
    rows = db.execute("SELECT best_shift, best_error, zero_error FROM fresh_lag_checks WHERE best_shift IS NOT NULL").fetchall()
    if not rows:
        return dict(rounds=0)
    shifts = sorted(r["best_shift"] for r in rows)
    zero = sorted(r["zero_error"] for r in rows if r["zero_error"] is not None)
    rec = db.execute("""SELECT count(*), avg(time-reference_timestamp), avg(abs(reference_price-spot_price)),
        avg(time-spot_timestamp) FROM fresh_recorder WHERE time>?""", (time.time() - 3600,)).fetchone()
    return dict(rounds=len(rows), typical_shift=shifts[len(shifts) // 2],
                typical_error_at_zero=zero[len(zero) // 2] if zero else None,
                exact_at_zero=sum(1 for z in zero if z < .01), recorded_last_hour=rec[0],
                reference_age=rec[1], brti_minus_spot=rec[2], spot_age=rec[3])


def accounts(db):
    out = []
    names = {"settlement_model": ("Settlement model (official rule)", "Official 60-second average rule · learned volatility · 30–70¢ · holds"),
             "settlement_live": ("Settlement model + live feeds", "Same official rule · live Coinbase/Kraken/Bitstamp/Gemini price instead of the delayed feed"),
             "limit_down_55": ("Limit order: DOWN near 55¢", "UP at 40–50¢ → rest a DOWN bid · only fills if traded through · holds")}
    for rule, (name, describe) in names.items():
        acct = db.execute("SELECT * FROM fresh_accounts WHERE rule=?", (rule,)).fetchone()
        rows = db.execute("SELECT pnl, pnl_no_fee FROM fresh_trades WHERE rule=? AND status='SETTLED'", (rule,)).fetchall()
        counts = dict(db.execute("SELECT status, count(*) FROM fresh_trades WHERE rule=? GROUP BY status", (rule,)).fetchall())
        pnl = [r["pnl"] for r in rows]
        out.append(dict(rule=rule, name=name, describe=describe, start=STARTING_CASH, stake=STAKE,
                        balance=acct["balance"] if acct else STARTING_CASH, trades=len(pnl),
                        wins=sum(p > 0 for p in pnl), losses=sum(p <= 0 for p in pnl), net=round(sum(pnl), 4),
                        net_no_fee=round(sum(r["pnl_no_fee"] for r in rows), 4),
                        orders=sum(counts.values()), cancelled=counts.get("CANCELLED", 0),
                        open=counts.get("FILLED", 0) + counts.get("RESTING", 0)))
    return out


def main_model(db):
    """'standard' (default), 'settlement' or 'settlement_live' - chosen by the owner."""
    row = db.execute("SELECT payload FROM state WHERE key='main_model'").fetchone()
    if row:
        return json.loads(row[0])
    row = db.execute("SELECT payload FROM state WHERE key='main_model_settlement'").fetchone()   # dev16 key
    return "settlement" if row and json.loads(row[0]) is True else "standard"


def main_uses_settlement(db):
    return main_model(db) != "standard"
