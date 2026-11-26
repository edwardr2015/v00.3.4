#!/usr/bin/env python3
"""Independently reproduce the primary-account cohort results from a Quick Report workbook.

Reads only the "Unified Trades" sheet (one row per primary paper position) and recomputes:
actual P&L, hold-to-settlement P&L, directional-confidence cohorts and entry-timing cohorts.

    python3 tools/reproduce_primary.py BTC-Prophet-Quick-Report-4.xlsx [--json out.json]

Definitions (stated so discrepancies can be traced):
  * win (actual)   = Paper P&L > 0
  * hold P&L       = quantity * (side == official outcome) - cost incl. fees
                     (settlement pays $1/contract, no exit fee). Uses the actual entry fill.
  * confidence     = UP estimate for UP entries, 1 - UP estimate for DOWN entries
  * entry second   = Entry UTC - market open parsed from the slug (...-HHMMz)
  * cohort results are RETROSPECTIVE filters on trades that already happened. A filtered
    cohort is not a forward result: it ignores markets the filter would have entered instead.
"""

import argparse
import json
import math
import re
from datetime import datetime, timezone

import openpyxl

SLUG_TIME = re.compile(r"(\d{4}-\d{2}-\d{2})-(\d{2})(\d{2})z$")


def market_open(slug):
    m = SLUG_TIME.search(slug)
    return datetime.strptime(f"{m[1]} {m[2]}:{m[3]}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)


def parse_time(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc)
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def load(path):
    ws = openpyxl.load_workbook(path, read_only=True, data_only=True)["Unified Trades"]
    rows = list(ws.iter_rows(values_only=True))
    head = rows[0]
    out = []
    for r in rows[1:]:
        d = dict(zip(head, r))
        if not d.get("Market") or d.get("Paper P&L") is None or d.get("Official outcome") not in ("UP", "DOWN"):
            continue
        p = float(d["UP estimate"])
        won_at_settle = d["Side"] == d["Official outcome"]
        cost = float(d["Cost incl. fees"])
        out.append(dict(
            market=d["Market"], side=d["Side"], status=d["Status"], model=d["Model"],
            pnl=float(d["Paper P&L"]), cost=cost,
            hold=float(d["Quantity"]) * won_at_settle - cost,
            confidence=p if d["Side"] == "UP" else 1 - p,
            second=(parse_time(d["Entry UTC"]) - market_open(d["Market"])).total_seconds(),
            settle_win=won_at_settle,
        ))
    return out


def wilson(k, n, z=1.96):
    if not n:
        return None, None
    p = k / n
    c = (p + z * z / (2 * n)) / (1 + z * z / n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return round(c - h, 4), round(c + h, 4)


def summarize(name, rows, key="pnl"):
    wins = sum(r[key] > 0 for r in rows)
    pnl = sum(r[key] for r in rows)
    staked = sum(r["cost"] for r in rows)
    peak = dd = run = 0.0
    for r in rows[::-1]:   # chronological (sheet is newest first)
        run += r[key]
        peak = max(peak, run)
        dd = max(dd, peak - run)
    lo, hi = wilson(wins, len(rows))
    return dict(cohort=name, trades=len(rows), wins=wins, net_pnl=round(pnl, 2),
                return_per_dollar=round(pnl / staked, 4) if staked else None,
                max_drawdown=round(dd, 2), win_ci95=[lo, hi])


def reproduce(rows):
    out = [summarize("Existing strategy (actual)", rows),
           summarize("Hold to settlement (same entries)", rows, "hold")]
    for t in (0.6, 0.7, 0.8, 0.9):
        out.append(summarize(f"Confidence >= {int(t*100)}%", [r for r in rows if r["confidence"] >= t]))
    out.append(summarize("Entry < 90 s", [r for r in rows if r["second"] < 90]))
    out.append(summarize("Entry >= 90 s", [r for r in rows if r["second"] >= 90]))
    sold = [r for r in rows if r["status"] == "SOLD"]
    out.append(summarize("Early-exited trades (actual)", sold))
    out.append(summarize("Early-exited trades if held", sold, "hold"))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("workbook")
    ap.add_argument("--json")
    a = ap.parse_args()
    res = reproduce(load(a.workbook))
    w = max(len(r["cohort"]) for r in res)
    print(f"{'Cohort':<{w}}  Trades  Wins     Net $   $/staked  MaxDD  Win 95% CI")
    for r in res:
        ci = "" if r["win_ci95"][0] is None else f"{r['win_ci95'][0]:.2f}-{r['win_ci95'][1]:.2f}"
        print(f"{r['cohort']:<{w}}  {r['trades']:>6}  {r['wins']:>4}  {r['net_pnl']:>8.2f}  "
              f"{r['return_per_dollar'] if r['return_per_dollar'] is not None else '':>8}  {r['max_drawdown']:>5}  {ci}")
    if a.json:
        open(a.json, "w").write(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
