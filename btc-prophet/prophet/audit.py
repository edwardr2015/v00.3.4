"""Version 2.0 system audit: measuring and tracing the whole bot, on demand.

Every check returns PASS, WARN or FAIL with the numbers behind it, so problems are visible without
anyone having to ask. Groups:
  safety        real money locked, no negative balances
  bookkeeping   every paper account reconciles: balance = start + settled P&L - money in open trades
  data          live exchange feeds healthy; delayed feed measured; settlement values verified
  positions     no trade left open long after its round ended
  model         main forecast calibration (overconfidence monitor)
  profit        each account's result with a 95% range; 'proven' only if 100+ trades and the whole
                range is above zero
  errors        warnings and errors in the event log over the last 24 hours
"""
import json
import math
import time

# (name, account table, account where, start cash, trades table, open statuses, cost expression, pnl column)
ACCOUNTS = [
    ("Main $1,000", "unified_account", "id=1", 1000.0, "unified_trades", ("OPEN",), "cost", "pnl", None),
    ("Selective $50", "selective50_account", "id=1", 50.0, "selective50_trades", ("OPEN",), "cost", "pnl", None),
    ("Selective $1,000 at $50", "selective1000_account", "id=1", 1000.0, "selective1000_trades", ("OPEN",), "cost", "pnl", None),
    ("1-hour fade", "hourly_account", "id=1", 50.0, "hourly_trades", ("OPEN",), "cost", "pnl", None),
]


def _check(name, group, status, detail, **numbers):
    return dict(name=name, group=group, status=status, detail=detail, numbers=numbers)


def _table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _range(returns):
    n = len(returns)
    if n < 2:
        return None, None
    mean = sum(returns) / n
    sd = math.sqrt(sum((r - mean) ** 2 for r in returns) / (n - 1))
    return mean - 1.96 * sd / math.sqrt(n), mean + 1.96 * sd / math.sqrt(n)


def _reconcile(db, label, acct, where, start, trades, open_statuses, cost, pnl, rule=None):
    row = db.execute(f"SELECT balance FROM {acct} WHERE {where}").fetchone()
    if row is None:
        return _check(f"{label} reconciles", "bookkeeping", "PASS", "No trades yet")
    flt = f" AND rule='{rule}'" if rule else ""
    settled = db.execute(f"SELECT coalesce(sum({pnl}),0) FROM {trades} WHERE {pnl} IS NOT NULL{flt}").fetchone()[0]
    marks = ",".join("?" * len(open_statuses))
    tied = db.execute(f"SELECT coalesce(sum({cost}),0) FROM {trades} WHERE status IN ({marks}){flt}",
                      open_statuses).fetchone()[0]
    expected = start + settled - tied
    gap = row[0] - expected
    status = "PASS" if abs(gap) < .01 else "FAIL"
    return _check(f"{label} reconciles", "bookkeeping", status,
                  f"balance ${row[0]:.2f} vs expected ${expected:.2f} (start ${start:.0f} + settled ${settled:+.2f} - open ${tied:.2f})",
                  balance=row[0], expected=expected, gap=gap)


def _profit(db, label, trades, cost, pnl, rule=None):
    flt = f" AND rule='{rule}'" if rule else ""
    rows = db.execute(f"SELECT {pnl} AS pnl, {cost} AS cost FROM {trades} WHERE {pnl} IS NOT NULL{flt}").fetchall()
    returns = [r["pnl"] / r["cost"] for r in rows if r["cost"]]
    n, net = len(returns), sum(r["pnl"] for r in rows)
    low, high = _range(returns)
    proven = n >= 100 and low is not None and low > 0
    status = "PASS" if proven else ("WARN" if n < 100 or (low is not None and low <= 0 < high) else "FAIL")
    if n >= 100 and high is not None and high < 0:
        status = "FAIL"
    verdict = ("PROVEN profitable" if proven else "losing (whole range below zero)" if high is not None and high < 0
               else "not proven yet" if n < 100 else "no edge shown (range includes zero)")
    return _check(f"{label} profit", "profit", status,
                  f"{n} settled · net ${net:+.2f} · per $1: {('%+.3f' % (sum(returns) / n)) if n else '—'}"
                  f"{'' if low is None else f' (95% range {low:+.3f} to {high:+.3f})'} · {verdict}",
                  trades=n, net=net, low=low, high=high, proven=proven)


def run(db, now=None, states=None):
    now = time.time() if now is None else now
    out = []

    def safe(fn):
        try:
            result = fn()
            out.extend(result if isinstance(result, list) else [result])
        except Exception as exc:  # noqa: BLE001 - one broken check must not hide the others
            out.append(_check(getattr(fn, "__name__", "check"), "errors", "FAIL", f"check crashed: {exc}"))

    # ---------- safety
    def mode():
        m = db.execute("SELECT payload FROM state WHERE key='mode'").fetchone()
        value = json.loads(m[0]) if m else "OFF"
        return _check("Paper engine stays paper", "safety", "PASS" if value in ("OFF", "LEARN", "SIMULATE") else "FAIL",
                      f"paper mode {value}; real orders only come from the separate live trader", mode=value)
    safe(mode)

    def negatives():
        bad = []
        for t in ("unified_account", "selective50_account", "selective1000_account", "shadow_rule_accounts",
                  "fresh_accounts", "hourly_account"):
            if _table(db, t):
                bad += [t for (b,) in db.execute(f"SELECT balance FROM {t}") if b < -1e-9]
        return _check("No negative balances", "safety", "PASS" if not bad else "FAIL",
                      "all paper accounts at or above $0" if not bad else "below $0: " + ", ".join(bad))
    safe(negatives)

    # ---------- bookkeeping
    for label, acct, where, start, trades, opens, cost, pnl, rule in ACCOUNTS:
        if _table(db, acct) and _table(db, trades):
            safe(lambda a=(label, acct, where, start, trades, opens, cost, pnl, rule): _reconcile(db, *a))
    if _table(db, "shadow_rule_accounts"):
        for (rule,) in db.execute("SELECT rule FROM shadow_rule_accounts").fetchall():
            safe(lambda r=rule: _reconcile(db, f"Test account {r}", "shadow_rule_accounts", f"rule='{r}'", 50.0,
                                           "shadow_rule_trades", ("OPEN",), "cost", "pnl", r))
    if _table(db, "fresh_accounts"):
        for (rule,) in db.execute("SELECT rule FROM fresh_accounts").fetchall():
            safe(lambda r=rule: _reconcile(db, f"Test account {r}", "fresh_accounts", f"rule='{r}'", 50.0,
                                           "fresh_trades", ("FILLED", "RESTING"), "quantity*price+fee", "pnl", r))

    # ---------- data
    def live_feeds():
        if not _table(db, "feed_ticks"):
            return _check("Live exchange feeds", "data", "WARN", "feed referee not running")
        row = db.execute("SELECT time, healthy FROM feed_ticks ORDER BY time DESC LIMIT 1").fetchone()
        if not row:
            return _check("Live exchange feeds", "data", "WARN", "no ticks yet")
        age, healthy = now - row[0], row[1]
        day = db.execute("SELECT avg(healthy>=2) FROM feed_ticks WHERE time>?", (now - 86400,)).fetchone()[0]
        status = "PASS" if age < 5 and healthy >= 3 else "WARN" if age < 30 and healthy >= 2 else "FAIL"
        return _check("Live exchange feeds", "data", status,
                      f"{healthy}/4 healthy, last tick {age:.0f}s ago, live price available {100 * (day or 0):.1f}% of last 24h",
                      healthy=healthy, age=age, availability=day)
    safe(live_feeds)

    def live_vs_official():
        if not _table(db, "feed_round_checks"):
            return _check("Live price matches official results", "data", "WARN", "no checks yet")
        rows = db.execute("SELECT best_shift, error_at_zero FROM feed_round_checks WHERE best_shift IS NOT NULL "
                          "ORDER BY end DESC LIMIT 100").fetchall()
        if len(rows) < 3:
            return _check("Live price matches official results", "data", "WARN", f"{len(rows)} rounds checked so far")
        shifts = sorted(r[0] for r in rows)
        errs = sorted(r[1] for r in rows if r[1] is not None)
        shift, err = shifts[len(shifts) // 2], errs[len(errs) // 2] if errs else None
        status = "PASS" if abs(shift) <= 4 and err is not None and err < 2 else "WARN" if abs(shift) <= 10 else "FAIL"
        return _check("Live price matches official results", "data", status,
                      f"typical time shift {shift:+d}s, typical error ${err:.2f} over {len(rows)} rounds (target 0 s, < $2)",
                      shift=shift, error=err, rounds=len(rows))
    safe(live_vs_official)

    def connections():
        out = []
        for name, label in (("reference", "Polymarket BRTI price"), ("market", "Polymarket order book"), ("spot", "Exchange spot price")):
            row = db.execute("SELECT payload FROM state WHERE key=?", ("health_" + name,)).fetchone()
            h = json.loads(row[0]) if row else {}
            last = h.get("last_success") or (h.get("checked") if h.get("ok") else None)
            age = now - last if last else None
            status = "PASS" if h.get("ok") and age is not None and age < 15 else "WARN" if age is not None and age < 120 else "FAIL"
            why = h.get("error") or ""
            hint = (" — the Mac may have slept or lost internet" if age and age > 120 else
                    " — Polymarket is limiting requests; it retries by itself" if "429" in str(why) else "")
            out.append(_check(f"{label} connected", "data", status,
                              f"last good update {('%.0fs ago' % age) if age is not None else 'never'}, "
                              f"{h.get('failures', 0)} failures in a row{(' · ' + str(why)[:120]) if why else ''}{hint}",
                              age=age, failures=h.get("failures", 0)))
        return out
    safe(connections)

    def delayed_feed():
        if not _table(db, "fresh_lag_checks"):
            return _check("Polymarket BRTI feed delay", "data", "WARN", "no checks yet")
        rows = db.execute("SELECT best_shift FROM fresh_lag_checks WHERE best_shift IS NOT NULL ORDER BY end DESC LIMIT 100").fetchall()
        if not rows:
            return _check("Polymarket BRTI feed delay", "data", "WARN", "no checks yet")
        s = sorted(r[0] for r in rows)[len(rows) // 2]
        return _check("Polymarket BRTI feed delay", "data", "PASS" if abs(s) <= 4 else "WARN",
                      f"typical shift {s:+d}s over {len(rows)} rounds" + ("" if abs(s) <= 4 else
                      " — this feed is late; decisions should use the live feeds"), shift=s)
    safe(delayed_feed)

    # ---------- positions
    def stuck():
        stuck = db.execute("""SELECT count(*) FROM unified_trades t JOIN unified_rounds r ON r.slug=t.slug
            WHERE t.status='OPEN' AND r.end < ?""", (now - 900,)).fetchone()[0] if _table(db, "unified_trades") else 0
        return _check("No stuck positions", "positions", "PASS" if not stuck else "WARN",
                      f"{stuck} main-account trades still open 15+ min after their round ended", stuck=stuck)
    safe(stuck)

    # ---------- model
    def calibration():
        rows = db.execute("""SELECT t.side, t.up, v.outcome FROM unified_trades t JOIN unified_rounds r ON r.slug=t.slug
            JOIN verified_markets v ON v.slug=t.slug AND v.end=r.end ORDER BY t.time DESC LIMIT 200""").fetchall()
        if len(rows) < 30:
            return _check("Main forecast is honest", "model", "WARN", f"{len(rows)} settled trades so far")
        said = sum((r["up"] if r["side"] == "UP" else 1 - r["up"]) for r in rows) / len(rows)
        won = sum(r["side"] == r["outcome"] for r in rows) / len(rows)
        gap = said - won
        status = "PASS" if abs(gap) < .04 else "WARN" if abs(gap) < .08 else "FAIL"
        return _check("Main forecast is honest", "model", status,
                      f"forecast said {100 * said:.0f}% average win chance, actually won {100 * won:.0f}% (last {len(rows)} trades)",
                      said=said, won=won, gap=gap)
    safe(calibration)

    # ---------- profit
    for label, acct, where, start, trades, opens, cost, pnl, rule in ACCOUNTS:
        if _table(db, trades):
            safe(lambda a=(label, trades, cost, pnl): _profit(db, *a))
    if _table(db, "shadow_rule_trades"):
        for (rule,) in db.execute("SELECT DISTINCT rule FROM shadow_rule_trades").fetchall():
            safe(lambda r=rule: _profit(db, f"Test account {r}", "shadow_rule_trades", "cost", "pnl", r))
    if _table(db, "fresh_trades"):
        for (rule,) in db.execute("SELECT DISTINCT rule FROM fresh_trades").fetchall():
            safe(lambda r=rule: _profit(db, f"Test account {r}", "fresh_trades", "quantity*price+fee", "pnl", r))

    # ---------- real money
    def real_money():
        from . import live
        if not _table(db, "live_orders"):
            return _check("Real-money trading", "safety", "PASS", "off")
        rows = db.execute("SELECT state, exit_state FROM live_orders WHERE mode='LIVE'").fetchall()
        errors = sum(1 for r in rows if r["state"] == "ERROR" or r["exit_state"] == "ERROR")
        l = live.ledger(db, now)
        status = "FAIL" if errors or l["cash"] < 0 else "PASS"
        return _check("Real-money trading", "safety", status,
                      f"{l['trades']} settled real trades, P&L ${l['pnl']:+.2f}, bot cash ${l['cash']:.2f} of $50, "
                      f"{l['open']} open, {errors} order errors", **l)
    safe(real_money)

    # ---------- errors
    def errors():
        rows = db.execute("SELECT level, count(*) FROM events WHERE time>? GROUP BY level", (now - 86400,)).fetchall()
        counts = {r[0]: r[1] for r in rows}
        e = counts.get("ERROR", 0)
        return _check("Errors in the last 24h", "errors", "PASS" if e == 0 else "WARN" if e < 50 else "FAIL",
                      f"{e} errors, {counts.get('WARNING', 0)} warnings", **counts)
    safe(errors)

    totals = {s: sum(c["status"] == s for c in out) for s in ("PASS", "WARN", "FAIL")}
    return dict(time=now, totals=totals, checks=out)
