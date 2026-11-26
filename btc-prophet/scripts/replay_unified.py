"""Replay the 0.3.21 $5 policy on a prior workbook's extracted JSONL evidence."""

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from prophet import unified
from prophet.execution import execute

FAMILY = "volatility_reference_gap"
COLUMNS = ("Scenario","Markets","Trades","Wins","Losses","Win rate","Skips","Early sales",
           "Net P&L","P&L per trade","ROI on staked","Max drawdown","Longest losing streak","Hold P&L on identical fills")


def items(folder, name):
    with (folder/(name+".jsonl")).open() as source:
        for line in source:
            yield json.loads(line)


def epoch(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()


def load(folder):
    results = {r["slug"]:r for r in items(folder,"UP DOWN History") if r.get("outcome") in ("UP","DOWN")}
    markets = defaultdict(list)
    for row in items(folder,"Autonomous Model Decisions"):
        slug = row["Market"]
        if row["Family"] != FAMILY or slug not in results or row.get("UP estimate") is None:
            continue
        d = json.loads(row["Evidence"])
        start,end,elapsed = d.get("market_open"),d.get("market_close"),row.get("Seconds after open")
        ref = (d.get("reference") or {}).get("timestamp")
        bid,ask,ts,checked = (d.get(k) for k in ("bid","ask","book_timestamp","book_checked"))
        if None in (start,end,elapsed,ref,bid,ask,ts,checked) or not d.get("terms"):
            continue
        now = start+elapsed
        if (start <= now < end and end == epoch(results[slug]["end UTC"])
                and ts <= now and 0 <= now-checked <= 10 and 0 <= now-ref <= 30
                and 0 < bid["price"] <= ask["price"] < 1 and min(bid["quantity"],ask["quantity"])>0):
            markets[slug].append(dict(now=now,start=start,end=end,checked=checked,raw=row["UP estimate"],
                                     book=dict(bids=[bid],asks=[ask]),terms=d["terms"]))
    for quotes in markets.values():
        quotes.sort(key=lambda q: q["now"])
    return markets, results


def probabilities(markets, results, slugs):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    unified.initialize(db)
    db.execute("CREATE TABLE verified_markets(slug TEXT PRIMARY KEY,end REAL,outcome TEXT,verified_at REAL)")
    for slug in slugs:
        quotes = markets[slug]
        for q in quotes:
            book = q["book"]
            midpoint = (book["bids"][0]["price"]+book["asks"][0]["price"])/2
            q["up"], q["matched"] = unified.calibrate(db,slug,FAMILY,q["raw"],midpoint,q["now"])
        q = quotes[0]
        db.execute("INSERT INTO unified_rounds VALUES (?,?,?,?,?,?,?,?)",
                   (slug,q["start"],q["end"],q["now"],"FORECAST",FAMILY,q["up"],"{}"))
        db.execute("INSERT INTO unified_forecasts VALUES (?,?,?,?,?)",(slug,FAMILY,q["now"],q["raw"],q["up"]))
        result = results[slug]
        db.execute("INSERT INTO verified_markets VALUES (?,?,?,?)",
                   (slug,q["end"],result["outcome"],epoch(result["verified_at UTC"])))
    db.close()


def choose(quotes, after, *, stake, edge, low, high, stress):
    for index,q in enumerate(quotes):
        if q["now"] < after:
            continue
        options = []
        for side,p in (("UP",q["up"]),("DOWN",1-q["up"])):
            c,_ = unified.candidate(q["book"],side,stake,p,q["terms"],low,high,edge,stressed=stress)
            if c:
                options.append((side,c))
        if options:
            side,c = max(options,key=lambda item:item[1]["ev"])
            return index,side,c["fill"]
    return None


def replay(name, markets, outcomes, held, *, stake=5, edge=.10, low=.40, high=.70, cashout=True, stress=False):
    cash = peak = 1000
    drawdown = longest = loss_run = after = 0
    rows = []
    for slug in held:
        qs = markets[slug]
        entry = choose(qs,after,stake=stake,edge=edge,low=low,high=high,stress=stress) if cash>=stake else None
        if not entry:
            rows.append((name,slug,"SKIP",None,None,None,None,None,None,None,None))
            continue
        index,side,fill = entry
        quote = qs[index]
        qty,cost = fill["quantity"],fill["total"]
        outcome = outcomes[slug]
        after,proceeds,status = epoch(outcome["verified_at UTC"]),qty*(side==outcome["outcome"]),"HOLD"
        if cashout:
            for later in qs[index+1:]:
                if later["checked"] <= quote["checked"]:
                    continue
                terms = dict(later["terms"])
                if stress:
                    terms.update(theta=min(1,terms["theta"]+.02),slippage_ticks=2)
                sale = execute(later["book"],side,quantity=qty,buying=False,**terms)
                probability = later["up"] if side=="UP" else 1-later["up"]
                if sale["quantity"]+1e-9>=qty and sale["total"]>=probability*qty+unified.EXIT_MARGIN*cost:
                    after,proceeds,status = later["now"],sale["total"],"SOLD"
                    break
        pnl = proceeds-cost
        cash += pnl
        peak = max(peak,cash)
        drawdown = max(drawdown,peak-cash)
        loss_run = loss_run+1 if pnl<0 else 0
        longest = max(longest,loss_run)
        hold_pnl = qty*(side==outcome["outcome"])-cost
        rows.append((name,slug,status,side,quote["now"],after,cost,fill["average_price"],pnl,outcome["outcome"],hold_pnl))
    trades = [r for r in rows if r[2]!="SKIP"]
    wins = sum(r[8]>0 for r in trades)
    losses = sum(r[8]<0 for r in trades)
    n,net = len(trades),cash-1000
    return (name,len(held),n,wins,losses,wins/n if n else None,len(held)-n,
            sum(r[2]=="SOLD" for r in trades),net,net/n if n else None,
            net/sum(r[6] for r in trades) if n else None,drawdown,longest,sum(r[10] for r in trades)),rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence",type=Path)
    parser.add_argument("output",type=Path)
    args = parser.parse_args()
    markets,results = load(args.evidence)
    slugs = sorted(markets,key=lambda slug:markets[slug][0]["start"])
    held = slugs[int(len(slugs)*.6):]
    if not held:
        raise SystemExit("No verified rounds with timestamped executable quote and forecast evidence.")
    probabilities(markets,results,slugs)
    wb = Workbook()
    sheet = wb.active
    sheet.title = "Replay"
    sheet.append(COLUMNS)
    audit = wb.create_sheet("Round audit")
    audit.append(("Scenario","Market","Action","Side","Entry epoch UTC","Exit epoch UTC",
                  "Cost incl. fees","Average entry price","P&L","Official outcome","Matched hold P&L"))
    scenarios = [
        ("$5 adaptive exit",{}),
        ("$5 hold policy",dict(cashout=False)),
        ("$5 adaptive exit, higher costs",dict(stress=True)),
        ("$5 hold policy, higher costs",dict(cashout=False,stress=True)),
        ("$5 lower EV hurdle, 3%",dict(edge=.03)),
        ("$5 no positive EV hurdle",dict(edge=0)),
        ("$5 wider price band",dict(low=.02,high=.85)),
        ("$100 same strategy, shadow",dict(stake=100)),
        ("$100 same strategy, higher costs",dict(stake=100,stress=True)),
    ]
    for name,policy in scenarios:
        result, rows = replay(name,markets,results,held,**policy)
        print(result)
        sheet.append(result)
        for row in rows:
            audit.append(row)
    method = wb.create_sheet("Method")
    for row in [
        ("Evidence","Archived Prophet Report 8 top-of-book quote and Reference Gap forecasts. No G30 ledger was supplied."),
        ("Order",f"{len(slugs)-len(held)} earlier markets warm up calibration. {len(held)} later distinct markets scored. Updates use only previously verified outcomes."),
        ("Scope","Tests the Reference Gap route of 0.3.21. Live evidence-based alternate model selection is excluded."),
        ("Costs","One adverse tick (two under stress), published fee coefficient with a .095 floor (+.02 under stress), 25% depth, 95% budget coverage."),
        ("Sizing","Each scenario starts with $1,000. The two $100 scenarios test size on the regular entry rule and do not unlock 0.3.21."),
        ("Exits","Only a full sale on a newer confirmed book when net proceeds exceed estimated hold value by 3% of cost. Hold comparison disables sales, so settlement latency can affect next entry."),
        ("Price filter","The wider price band retains the 85¢ all-in cap."),
    ]:
        method.append(row)
    for ws in wb.worksheets:
        ws.freeze_panes = "B2"
        for cell in ws[1]:
            cell.fill = PatternFill("solid",fgColor="172A40")
            cell.font = Font(color="FFFFFF",bold=True)
        ws.column_dimensions["A"].width=40
    method.column_dimensions["B"].width=110
    wb.save(args.output)


if __name__ == "__main__":
    main()
