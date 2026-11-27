"""Streaming Excel audit from a single SQLite snapshot; no fictional results."""

import io
import math
import json
import os
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path
from collections import defaultdict
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.chart import LineChart, Reference
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from .reporting import safe, utc
from .analytics import streaks
from .v2risk import local_day
from . import agreement, competition, viability, exit_research, hold_research, model_paper, observer, pooled_consensus, research_audit, selective_forecast, unified, corrective
from .lab import timing_bankroll
from .models import VOTE_VERSION
from .adaptive_entry import STARTING_CASH, get_state, open_position_marks

MAX_ROWS = 1048576
TIMESTAMPS = {
    "time",
    "settled",
    "created",
    "trained",
    "verified_at",
    "start",
    "end",
    "finalized",
    "target",
    "book_timestamp",
    "training_end",
    "validation_start",
    "approved",
}


# Sheets of retired or superseded features (flips, smart exits, old adaptive accounts, roulette).
# They are omitted from the Full audit only when they have no rows; core sheets always appear.
LEGACY_OPTIONAL = {
    "Roulette Pick History", "Smart Flip Research", "Smart Flip Actions", "Adaptive Selections",
    "Adaptive Decisions", "Adaptive Market Windows", "Adaptive Labeled Observations",
    "Later Entry Comparison", "Hold vs Flip Rounds", "Paper Flips", "Unfunded Adaptive Flips",
    "Paired Shadow Stakes", "Approved Configurations", "Experiment History", "Trend Analyses",
    "Human Review", "Research Runs", "Adaptive vs Fixed Timing",
}


class Sheets:
    def __init__(self, wb):
        self.wb = wb
        self.counts = {}

    def create(self, name, headers):
        ws = self.wb.create_sheet(name)
        ws.freeze_panes = "A2"
        for i, header in enumerate(headers, 1):
            ws.column_dimensions[get_column_letter(i)].width = min(48, max(16, len(header) + 3))
        row = []
        for header in headers:
            cell = WriteOnlyCell(ws, value=header)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="17243A")
            row.append(cell)
        ws.append(row)
        self.counts[ws.title] = 1
        return ws

    def append(self, ws, values, formula_column=None):
        row = []
        for i, value in enumerate(values, 1):
            value = safe(value) if i != formula_column else value
            if isinstance(value, str) and len(value) > 32767:
                value = (
                    value[:32670] + " [AUDIT JSON TRUNCATED FOR EXCEL; FULL RECORD IS IN SQLITE]"
                )
            if isinstance(value, float):
                cell = WriteOnlyCell(ws, value=value)
                cell.number_format = "0.0000;[Red]-0.0000"
                value = cell
            row.append(value)
        ws.append(row)
        self.counts[ws.title] += 1

    def prune_empty(self):
        """Drop empty legacy sheets and list them on one sheet (PROPHET_REPORT_KEEP_EMPTY=1 keeps all)."""
        import os
        if os.getenv("PROPHET_REPORT_KEEP_EMPTY") == "1":
            return []
        empty = [ws for ws in self.wb._sheets if self.counts.get(ws.title) == 1 and ws.title in LEGACY_OPTIONAL]
        for ws in empty:
            ws.close()                   # finish the write-only stream before dropping the sheet
            self.wb._sheets.remove(ws)
        if empty:
            self.table("Omitted Empty Sheets", ["Sheet", "Reason"],
                       ([ws.title, "No rows in this database"] for ws in empty))
        return [ws.title for ws in empty]

    def table(self, name, headers, rows):
        ws = self.create(name, headers)
        first = ws
        part = 1
        for row in rows:
            if self.counts[ws.title] >= MAX_ROWS:
                ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{self.counts[ws.title]}"
                part += 1
                ws = self.create(name[:26] + f" {part}", headers)
            self.append(ws, row)
        ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{self.counts[ws.title]}"
        return first


def workbook(store, paper=None):
    output = io.BytesIO()
    write_workbook(store.path, output, include_account_equity=bool(paper))
    return output.getvalue()


def add_unified(sheets, db, *, quick):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='unified_account'").fetchone():
        return
    from .learning import matched_forecasts

    now = time.time()
    limit = QUICK_LIMIT if quick else -1
    account = unified.summary(db,now)
    if account is None:
        return
    gate = unified.matched_large(db,now)
    c = matched_forecasts(db,now)
    sheets.table("Unified Scoreboard", ["Metric","Value"], [
        ["Started UTC",utc(account["start"])],["Starting cash",unified.STARTING_CASH],
        ["Current cash",account["balance"]],["Counting since UTC",utc(account["counting_since"])],
        ["Games",account["played"]],["Wins",account["wins"]],
        ["Losses",account["losses"]],["Net P&L",account["net"]],
        ["All-time games",account["all_time"]["played"]],["All-time wins",account["all_time"]["wins"]],
        ["All-time losses",account["all_time"]["losses"]],["All-time net P&L",account["all_time"]["net"]],
        ["$100 matched shadow fills",gate["rounds"]],["$100 after higher costs",gate["stress_net"]],
        ["$100 eligible",gate["qualified"]],["Matched challenger forecasts",c["matched"]],
        ["Baseline correct",c["baseline_right"]],["Challenger correct",c["challenger_right"]],
        ["Baseline Brier",c["baseline_brier"]],["Challenger Brier",c["challenger_brier"]],
    ])
    sheets.table("Unified Trades", ["Market","Entry UTC","Side","Model","UP estimate","Budget","Quantity",
        "Cost incl. fees","Entry fee","Status","Exit UTC","Proceeds","Paper P&L","Official outcome",
        "Matched hold P&L","Exit minus hold","Entry evidence","Exit evidence"], (
        [r["slug"],utc(r["time"]),r["side"],r["model"],r["up"],r["stake"],r["quantity"],r["cost"],
         r["fee"],r["status"],utc(r["closed"]),r["proceeds"],r["pnl"],r["outcome"],r["hold_pnl"],
         r["pnl"]-r["hold_pnl"] if r["pnl"] is not None and r["hold_pnl"] is not None else None,
         r["details"],r["exit_details"]] for r in unified.history(db,limit)))
    sheets.table("Unified Observations", ["Market","Last observation UTC","Result","Model","UP estimate","Evidence"], (
        [r["slug"],utc(r["observed"]), "SKIP: "+r["reason"] if r["end"]<=now and r["reason"]!="ENTERED"
         else r["reason"],r["model"],r["up"],r["details"]]
        for r in db.execute("SELECT * FROM unified_rounds ORDER BY start DESC LIMIT ?",(limit,))))
    from . import selective50
    sel = selective50.summary(db)
    sheets.table("Selective $50 Account", ["Market","Entry UTC","Side","Quantity","Cost","Status","Exit UTC",
        "Proceeds","Paper P&L","Official outcome","Evidence"], [
        ["Summary",sel["name"],"Balance",sel["balance"],"Trades",sel["trades"],"Wins",sel["wins"],
         "Losses",sel["losses"],"Net "+str(sel["net"])]] + [
        [r["slug"],utc(r["time"]),r["side"],r["quantity"],r["cost"],r["status"],utc(r["closed"]) if r["closed"] else None,
         r["proceeds"],r["pnl"],r["outcome"],r["details"]]
        for r in db.execute("SELECT * FROM selective50_trades ORDER BY time DESC LIMIT ?",(limit,))])
    big = selective50.summary(db, "selective1000")
    sheets.table("Selective $1000 at $50", ["Market","Entry UTC","Side","Quantity","Cost","Status","Exit UTC",
        "Proceeds","Paper P&L","Official outcome","Evidence"], [
        ["Summary",big["name"],"Balance",big["balance"],"Trades",big["trades"],"Wins",big["wins"],
         "Losses",big["losses"],"Net "+str(big["net"])]] + [
        [r["slug"],utc(r["time"]),r["side"],r["quantity"],r["cost"],r["status"],utc(r["closed"]) if r["closed"] else None,
         r["proceeds"],r["pnl"],r["outcome"],r["details"]]
        for r in db.execute("SELECT * FROM selective1000_trades ORDER BY time DESC LIMIT ?",(limit,))])
    from . import shadow_rules
    names = {k: v["label"] for k, v in shadow_rules.RULES.items()}
    sheets.table("Test Accounts", ["Account","Market","Entry UTC","Side","Quantity","Cost","Status","Exit UTC",
        "Proceeds","Paper P&L","Official outcome","Evidence"], [
        ["Summary: "+a["name"],a["describe"],"Balance",a["balance"],"Trades",a["trades"],"Wins",a["wins"],
         "Losses",a["losses"],"Net",a["net"],"Sold early "+str(a["early_exits"])] for a in shadow_rules.summary(db)] + [
        [names.get(r["rule"],r["rule"]),r["slug"],utc(r["time"]),r["side"],r["quantity"],r["cost"],r["status"],
         utc(r["closed"]) if r["closed"] else None,r["proceeds"],r["pnl"],r["outcome"],r["details"]]
        for r in db.execute("SELECT * FROM shadow_rule_trades ORDER BY time DESC LIMIT ?",(limit,))])
    from . import feed_check
    fc = feed_check.summary(db)
    sheets.table("Price Feed Check", ["Market","Start UTC","Official open","Bot open","Open difference $",
        "Official close","Bot last reading","Last reading at (s)","Feed age (s)","Outcome","Bot direction matched"], [
        ["Summary","Rounds "+str(fc.get("rounds",0)),"Median open gap $",fc.get("median_open_gap"),
         "Worst open gap $",fc.get("worst_open_gap"),"Minute-4 direction right",fc.get("minute4_direction_right"),
         "Median feed age (s)",fc.get("median_feed_age"),None]] + [
        [r["slug"],utc(r["start"]),r["official_open"],r["bot_open"],r["open_difference"],r["official_close"],
         r["bot_last"],r["last_elapsed"],r["feed_age"],r["outcome"],r["bot_direction_matches"]]
        for r in feed_check.rows(db, limit)])
    sheets.table("Exit Shadow Comparison", ["Exit policy","Settled trades","Shadow exits","Wins","Net P&L",
        "Primary exit setting"], (
        [r["policy"],r["trades"],r["exits"],r["wins"],r["pnl"],unified.primary_exit()]
        for r in unified.exit_comparison(db)))
    sheets.table("Model Selection Evidence", ["Market","Regime","UTC","Model","Evidence"], (
        [r["slug"],r["regime"],utc(r["time"]),r["model"],r["evidence"]]
        for r in db.execute("SELECT * FROM paper_model_choices ORDER BY time DESC LIMIT ?",(limit,))))
    if not quick:
        sheets.table("Unified Large Shadows", ["Market","Quote UTC","Side","Cost","Stress cost","Outcome",
            "P&L","Stress P&L","Evidence"], (
            [r["slug"],utc(r["time"]),r["side"],r["cost"],r["stress_cost"],r["outcome"],
             (r["side"]==r["outcome"])*r["quantity"]-r["cost"] if r["outcome"] else None,
             (r["side"]==r["outcome"])*r["stress_quantity"]-r["stress_cost"] if r["outcome"] else None,r["details"]]
            for r in db.execute("""SELECT p.*,v.outcome FROM unified_large_trials p
                JOIN unified_rounds r ON r.slug=p.slug LEFT JOIN verified_markets v ON v.slug=p.slug AND v.end=r.end
                ORDER BY p.time DESC""")))


def add_entry_pricing(sheets, db, *, quick):
    rows = db.execute("""SELECT p.family,p.slug,p.side,p.entered,p.cost,p.quantity,p.entry_fee,
           p.outcome,p.pnl, CASE WHEN p.side='UP' THEN json_extract(d.evidence,'$.ask.price')
             ELSE 1-json_extract(d.evidence,'$.bid.price') END AS quoted_ask
        FROM model_paper_positions p
        LEFT JOIN model_paper_decisions d
          ON d.family=p.family AND d.slug=p.slug AND d.time=p.entered AND d.action=p.side
        WHERE p.quantity>0 ORDER BY p.entered DESC LIMIT ?""",(QUICK_LIMIT if quick else -1,))
    seen = set()

    def values():
        for r in rows:
            key = (r["family"],r["slug"])
            if key in seen:
                continue
            seen.add(key)
            price = (r["cost"]-r["entry_fee"])/r["quantity"]
            all_in = r["cost"]/r["quantity"]
            yield [r["family"],r["slug"],r["side"],utc(r["entered"]),r["quoted_ask"],price,
                   all_in,r["entry_fee"],r["quantity"],r["outcome"],r["pnl"],
                   .40-1e-10<=price<=.70+1e-10,.50-1e-10<=price<=.70+1e-10]
    sheets.table("Entry Pricing Audit", ["Model","Market","Side","Entry UTC","Quoted ask",
        "Execution price before fees","All-in cost per contract","Fee","Quantity","Outcome","Net P&L",
        "40–70¢ before fees","50–70¢ before fees"],values())


def add_selective_blockers(sheets, db):
    sheets.table("Selective 90 Blockers", ["Stage","Reason","Rounds"], (
        [r["stage"],r["reason"],r["rounds"]] for r in db.execute("""
          SELECT CASE WHEN status='CALL' THEN 'Fill' ELSE 'Direction' END AS stage,
                 CASE WHEN status='CALL' THEN coalesce(fill_reason,'NO FILL REASON RECORDED')
                      ELSE coalesce(reason,status) END AS reason, count(*) AS rounds
          FROM selective_forecasts GROUP BY stage,reason ORDER BY stage,rounds DESC,reason""")))


def add_corrective(sheets, db, *, quick):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='corrective_rounds'").fetchone():
        return
    data = corrective.summary(db,time.time())
    rows = [dict(data["primary"],policy="Existing primary",cost_case="actual paper"),*data["policies"]]
    sheets.table("Forward Research", ["Policy","Costs","Same verified markets","Trials","Wins","Losses",
        "Net P&L","Mean P&L","Drawdown","Skips","95% win CI low","95% win CI high"], (
        [r["policy"],r["cost_case"],data["verified_markets"],r["trades"],r["wins"],r["losses"],
         r["pnl"],r["per_trade"],r["drawdown"],r.get("skips"),*(r["win_interval"] or (None,None))]
        for r in rows))
    from . import late_exit_research

    exits = list(late_exit_research.results(db,time.time()))
    sheets.table("Late Exit Comparison", ["Costs","Matched fills","Executed exits","Hold P&L","+240s P&L","Exit minus hold"], (
        [case,len(rows),sum(r["proceeds"] is not None for r in rows),sum(r["hold_pnl"] for r in rows),
         sum(r["late_pnl"] for r in rows),sum(r["late_pnl"]-r["hold_pnl"] for r in rows)]
        for case in ("standard","higher") if (rows:=[r for r in exits if r["cost_case"]==case])))
    if quick:
        return
    sheets.table("Late Exit Audit", ["Market","Costs","Side","Entry UTC","Exit UTC","Result","Outcome",
        "Hold P&L","Late sale P&L","Quote evidence"], (
        [r["slug"],r["cost_case"],r["side"],utc(r["entry_time"]),utc(r["exit_time"]),
         r["reason"] or "NO SALE OBSERVED; HELD",r["outcome"],r["hold_pnl"],r["late_pnl"],r["evidence"]]
        for r in exits))
    sheets.table("Forecast Reliability", ["Model","UP probability band","Markets","Mean UP","Observed UP"], (
        [r["model"],f'{r["band"]*10}–{(r["band"]+1)*10}%',r["count"],r["mean_up"],r["observed_up"]]
        for r in data["calibration"]))
    sheets.table("Model Error Correlation", ["First model","Second model","Matched markets","Both wrong","Error correlation"], (
        [r["first"],r["second"],r["markets"],r["both_wrong"],r["error_correlation"]] for r in data["dependence"]))
    sheets.table("Synchronized Forecasts", ["Market","Target seconds","Decision UTC","Panel evidence"], (
        [r["slug"],r["checkpoint"],utc(r["time"]),r["payload"]]
        for r in db.execute("SELECT * FROM corrective_panels ORDER BY time")))
    sheets.table("Independent Model Scores", ["Checkpoint","Model","Count","Correct","Brier","Log loss"], (
        [r["checkpoint"],r["model"],r["count"],r["correct"],r["brier"],r["log_loss"]] for r in data["models"]))
    sheets.table("Lightweight Challengers", ["Model","Verified forecasts","Correct","Brier","Log loss"], (
        [r["model"],r["count"],r["correct"],r["brier"],r["log_loss"]] for r in data["challengers"]))
    sheets.table("Trade Quality Evidence", ["Market","Checkpoint","Model","Decision UTC","UP estimate","Evidence"], (
        [r["slug"],r["checkpoint"],r["model"],utc(r["time"]),r["up"],r["context"]]
        for r in db.execute("SELECT * FROM corrective_challengers ORDER BY time")))
    sheets.table("Forward Quote Audit", ["Market","Decision UTC","Evidence and rejections"], (
        [r["slug"],utc(r["time"]),r["evidence"]]
        for r in db.execute("SELECT * FROM corrective_observations ORDER BY id")))
    sheets.table("Forward Fill Audit", ["Market","Policy","Costs","Decision UTC","Direction","Size","Cost","Evidence"], (
        [r["slug"],r["policy"],r["cost_case"],utc(r["time"]),r["side"],r["quantity"],r["cost"],r["evidence"]]
        for r in db.execute("SELECT * FROM corrective_trials ORDER BY time")))


def write_workbook(database, output, *, include_account_equity=True):
    wb = Workbook(write_only=True)
    sheets = Sheets(wb)
    uri = Path(database).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=10)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        add_unified(sheets,db,quick=False)
        add_corrective(sheets,db,quick=False)
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='model_paper_positions'").fetchone():
            add_entry_pricing(sheets,db,quick=False)
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        experiment = get_state(db, "v2_experiment") or {}
        mode = get_state(db, "mode") or "OFF"
        forecasts, families, last_forecast = (0, 0, None)
        if "lab_predictions" in names:
            participants = tuple(competition.PARTICIPANTS)
            marks = ",".join("?" for _ in participants)
            forecasts, families, last_forecast = db.execute(f"""SELECT count(*),count(DISTINCT config_family),max(time)
                FROM lab_predictions WHERE up IS NOT NULL AND config_family IN ({marks})""", participants).fetchone()
        learning = get_state(db, "learning_status") or {}
        dash = sheets.table(
            "Performance Dashboard",
            ["Metric", "Value"],
            [
                ["Scope", "PAPER / SHADOW ONLY; no real-money execution"],
                ["Current decision mode", mode],
                ["Challenger learning status", learning.get("status", "WAITING FOR FIRST TRAINING CHECK")],
                ["Challenger version", learning.get("version")],
                ["Challenger training examples", learning.get("examples")],
                ["Challenger training note", learning.get("message")],
                ["Activity", {"OFF": "Decisions paused; collection and settlement may continue",
                              "LEARN": "Recording forecasts and unfunded research",
                              "SIMULATE": "Recording forecasts and modeling paper entries"}.get(mode)],
                ["Competition forecasts recorded", forecasts],
                ["Competition model families with forecasts", families],
                ["Last competition forecast UTC", utc(last_forecast)],
                ["Formal seven-day experiment", experiment.get("status", "NOT STARTED")],
                ["Formal start UTC", utc(experiment.get("start"))],
                ["Formal end UTC", utc(experiment.get("end"))],
                ["Trade records", db.execute("SELECT count(*) FROM v2_orders").fetchone()[0]],
                [
                    "Empty tables",
                    "No rows means no recorded results; it is not a profitable experiment",
                ],
                ["Paper accounts", "$1,000 per new account; adaptive paper positions hold to settlement; new flips are shadow research"],
                ["Costs", "25% observed depth, 1 adverse tick, theta >= .095, no rebates"],
                ["Historical profitability", "UNKNOWN when causal entry/exit quotes are missing"],
            ],
        )

        def raw(sheet, table):
            if table not in names:
                return sheets.table(sheet, ["Status"], [])
            headers = [r["name"] for r in db.execute("PRAGMA table_info(" + table + ")")]
            rows = (
                [utc(r[k]) if k in TIMESTAMPS and r[k] is not None else r[k] for k in headers]
                for r in db.execute("SELECT * FROM " + table)
            )
            return sheets.table(
                sheet, [h + " UTC" if h in TIMESTAMPS else h for h in headers], rows
            )

        if "agreement_research" in names:
            sheets.table("Observer Rules", ["Rule", "Value"], [
                ["Starting cash per account", observer.STARTING_CASH],
                ["Fixed entry budget", observer.ENTRY_BUDGET],
                ["Daily loss cap", "Disabled"],
                ["Consecutive losses before Learn", observer.LOSS_STREAK],
                ["Recovery", "Next correct official outcome from an eligible pick made after the pause"],
                ["Exit", "Official settlement"],
            ])
            sheets.table("Agreement Scenarios",
                         ["Scenario","Entry seconds","Starting cash","Cash available","Entry budget",
                          "Daily loss limit","Loss streak limit","Minimum after-cost edge",
                          "Observed rounds","Officially settled rounds","Paper trades",
                          "Wins","Losses","Skips","Win rate","Net paper P&L",
                          "Rounds with either fill","P&L vs same-time control"], (
                [r[k] for k in ("label","checkpoint","starting_cash","cash","budget","daily_limit",
                                "streak_limit","min_edge","observed","settled_rounds","trades","wins","losses",
                                "skips","win_rate","net_pnl","paired_active","vs_control")]
                for r in agreement.summary(db)
            ))
            sheets.table("Agreement Round Audit",
                         ["Market","Entry seconds","Scenario","Observation UTC","Model","Model UP score",
                          "Predicted side","Opening reference","Closed candle price","Move (basis points)",
                          "Candle end UTC","Book source UTC","Book confirmed UTC","Eligible quantity",
                          "Eligible cost","Estimated after-cost edge","Filter reason","Account reason",
                          "Played quantity","Played cost","Played fee","Official outcome","Market close UTC",
                          "Net paper P&L"], (
                [r["slug"],r["checkpoint"],r["scenario"],utc(r["time"]),r["model"],r["prediction_up"],
                 r["direction"],r["opening_price"],r["candle_close"],r["gap_bps"],utc(r["candle_end"]),
                 utc(r["book_timestamp"]),utc(r["book_checked"]),r["quantity"],r["cost"],r["edge_rate"],
                 r["reason"],r["policy_reason"],r["played_quantity"],r["played_cost"],r["played_fee"],
                 r["outcome"],utc(r["settlement_end"]),r["pnl"]]
                for r in db.execute(agreement.results_sql()+" ORDER BY r.time,r.slug,r.checkpoint,r.scenario")
            ))
            raw("Observer Account Ledger", "observer_rounds")
            sheets.table("Shadow Bias Summary",
                         ["Signal","Entry seconds","Matched settled rounds","Correct calls",
                          "Accuracy","Agreed with model"], (
                [r[k] for k in ("signal","checkpoint","rounds","correct","win_rate","model_agreement")]
                for r in research_audit.bias_summary(db)
            ))
            sheets.table("Shadow Bias Round Audit",
                         ["Market","Entry seconds","Observation UTC","Momentum UP score",
                          "Book midpoint UP","BTC move from open (bps)","Momentum side","Market odds side",
                          "Reference trend side","Official outcome","Momentum right","Market odds right",
                          "Reference trend right","Observed signal agreement"], (
                [r["slug"],r["checkpoint"],utc(r["time"]),r["momentum_up"],r["odds_up"],r["gap_bps"],
                 r["momentum"],r["market_odds"],r["reference_trend"],r["outcome"],
                 r["momentum_correct"],r["market_odds_correct"],r["reference_trend_correct"],
                 "All three agreed" if r["momentum"] and r["momentum"]==r["market_odds"]==r["reference_trend"]
                 else "Signals differed or tied"]
                for r in db.execute(research_audit.bias_sql()+" ORDER BY time,checkpoint")
            ))
            skips = research_audit.skip_diagnostics(db)
            sheets.table("Recent Skip Reasons", ["Scope","Account or model","Reason","Count"], (
                [scope,r.get("account") or r.get("model"),r["reason"],r.get("rounds",r.get("attempts"))]
                for scope in ("adaptive", "fixed") for r in skips[scope]
            ))

        if {"lab_configs","hold_flip_inputs"} <= names:
            import time
            scores = competition.standings(db,time.time(),[])
            sheets.table("Nine Model Leaderboard",[
                "Participant","Version","Games","Wins","Losses","Skips","Win rate","Cash","Equity","Realized",
                "Unrealized","Net","Exposure","Drawdown","Current","Longest wins","Longest losses"
            ], ([r[k] for k in ("label","version","games","wins","losses","skips","win_rate","cash","equity",
                 "realized","unrealized","net_pnl","exposure","drawdown")]+
                 [r["streak"]["current"],r["streak"]["longest"]["WIN"],r["streak"]["longest"]["LOSS"]] for r in scores))
            sheets.table("Roulette Pick History",["Market","UTC","Model","UP estimate","Decision","Outcome"],(
                [r["slug"],utc(r["time"]),r["model"],r["up"],r["decision"],r["outcome"]]
                for r in db.execute("""SELECT d.*,v.outcome FROM adaptive_decisions d LEFT JOIN verified_markets v
                    ON v.slug=d.slug WHERE d.account='adaptive-1' AND d.up IS NOT NULL ORDER BY d.time,d.id""")))
        if "pattern_forecasts" in names:
            forecasts = [dict(r) for r in db.execute("""SELECT p.*,v.outcome FROM pattern_forecasts p
                LEFT JOIN verified_markets v ON v.slug=p.slug AND p.time<v.end ORDER BY p.time,p.checkpoint""")]
            for r in forecasts:
                r["parsed"]=json.loads(r["evidence"])
            sheets.table("Pattern Odds Predictions",[
                "Market","Checkpoint","UTC","Model","UP estimate","DOWN estimate","Official outcome",
                "Frequency baseline","Market baseline","Momentum baseline","Brier error","Log loss","Status","Audit"
            ], (
                [r["slug"],r["checkpoint"],utc(r["time"]),r["version"],r["up"],1-r["up"] if r["up"] is not None else None,
                 r["outcome"],base.get("frequency"),base.get("market"),base.get("momentum"),
                 (r["up"]-int(r["outcome"]=="UP"))**2 if r["up"] is not None and r["outcome"] else None,
                 -math.log(r["up"] if r["outcome"]=="UP" else 1-r["up"])
                   if r["up"] is not None and r["outcome"] else None,
                 r["parsed"].get("reason","PREDICTION"),r["evidence"]]
                for r in forecasts for base in [r["parsed"].get("baselines",{})]))
            sheets.table("Sequence Analysis",["Market","UTC","Order","Sequence","Matches","UP estimate","Uncertainty SD"],(
                [r["slug"],utc(r["time"]),m["order"]," ".join(m["sequence"]),m["occurrences"],m["up_probability"],m["sd"]]
                for r in forecasts for m in r["parsed"].get("sequence",{}).get("suffixes",[])))
            sheets.table("Transition Probabilities",["Market","UTC","From","To","Probability"],(
                [r["slug"],utc(r["time"]),a,b,p] for r in forecasts
                for a,transitions in r["parsed"].get("sequence",{}).get("transition",{}).items()
                for b,p in transitions.items()))
            sheets.table("Technical Indicators",["Market","UTC","Indicators","Regime","Volatility-adjusted distance"],(
                [r["slug"],utc(r["time"]),json.dumps(ind),ind.get("regime"),r["parsed"].get("volatility_adjusted_distance")]
                for r in forecasts for ind in [r["parsed"].get("indicators",{})]))
            sheets.table("Odds and Expected Value",["Market","UTC","Bid","Ask","Spread","Depth bid","Depth ask","EV UP","EV DOWN"],(
                [r["slug"],utc(r["time"]),*(r["parsed"].get(k) for k in ("bid","ask","spread","bid_depth","ask_depth")),
                 (r["parsed"].get("ev") or {}).get("UP"),(r["parsed"].get("ev") or {}).get("DOWN")]
                for r in forecasts))
        if "lab_counterfactuals" in names:
            sheets.table("SKIP Counterfactuals",["Configuration","Market","Side","Capture UTC","Quantity",
                "Cost","Evidence status","Official outcome","Unfunded P&L"],(
                [r["config"],r["slug"],r["side"],utc(r["time"]),r["quantity"],r["cost"],
                 r["reason"],r["outcome"],(r["quantity"] if r["side"]==r["outcome"] else 0)-r["cost"]
                 if r["quantity"] is not None and r["outcome"] else None]
                for r in db.execute("""SELECT s.*,v.outcome FROM lab_counterfactuals s LEFT JOIN verified_markets v
                    ON s.slug=v.slug AND s.time<v.end ORDER BY s.time,s.config,s.side""")))
        raw("Strategy Configurations","lab_configs")

        trade_headers = [
            "ID",
            "Account",
            "Market",
            "Side",
            "Entry UTC",
            "Model",
            "Estimated UP",
            "Quantity",
            "Total cost incl fees",
            "Fee",
            "Status",
            "Official outcome",
            "Net P&L",
            "Settlement UTC",
            "Audit JSON",
            "Strategy segment",
            "P&L audit formula",
            "Position side at settlement",
            "Contracts at settlement",
            "Flip sale proceeds",
            "Flip purchase cost",
        ]
        trades = sheets.create("Trade History", trade_headers)
        for i, r in enumerate(db.execute("""SELECT o.*, coalesce(f.side,o.side) AS final_side,
            coalesce(f.quantity,o.quantity) AS final_qty, coalesce(f.credit,0) AS flip_credit,
            coalesce(f.debit,0) AS flip_debit
            FROM v2_orders o LEFT JOIN paper_flips f ON f.order_id=o.id ORDER BY o.id"""), 2):
            sheets.append(
                trades,
                [
                    r["id"],
                    r["account"],
                    r["slug"],
                    r["side"],
                    utc(r["time"]),
                    r["model"],
                    r["probability"],
                    r["quantity"],
                    r["cost"],
                    r["fee"],
                    r["status"],
                    r["result"],
                    r["pnl"],
                    utc(r["settled"]),
                    r["details"],
                    r["segment"],
                    f'=IF(K{i}="SETTLED",IF(R{i}=L{i},S{i},0)-I{i}+T{i}-U{i},"")',
                    r["final_side"],r["final_qty"],r["flip_credit"],r["flip_debit"],
                ],
                formula_column=17,
            )
        raw("Skipped Markets", "v2_skips")
        raw("UP DOWN History", "verified_markets")
        raw("Strategy Accounts", "v2_accounts")
        raw("Learning History", "model_runs")
        raw("Strategy Change History", "strategy_changes")
        raw("Trend Analyses", "trend_runs")
        raw("Risk Events", "risk_events")
        raw("Repair History", "repair_history")
        sheets.table("Experiment History", ["Record", "Classification", "Start UTC", "End UTC", "Record JSON"], (
            [r["key"], "Preflight recorded" if x.get("preflight") else "Unverified legacy comparison",
             utc(x.get("start")), utc(x.get("end")), r["payload"]]
            for r in db.execute("""SELECT key,payload FROM state WHERE key='v2_experiment'
              OR key LIKE 'pre_%_experiment' OR key LIKE 'archived_%_experiment_%' ORDER BY received""")
            for x in [json.loads(r["payload"])] if isinstance(x,dict)
        ))
        if "operational_incidents" in names:
            sheets.table("Reliability and Downtime",["Issue","Opened UTC","Last observed UTC","Resolved UTC","Duration seconds","Details"],(
                [r["kind"],utc(r["opened"]),utc(r["last_seen"]),utc(r["resolved"]),
                 (r["resolved"] or r["last_seen"])-r["opened"],r["details"]]
                for r in db.execute("SELECT * FROM operational_incidents ORDER BY id")))

        raw("Predictions", "v2_decisions")
        raw("Hourly Checkpoints", "v2_hourly")
        raw("Adaptive Selections", "adaptive_selections")
        raw("Adaptive Decisions", "adaptive_decisions")
        raw("Paper Model Selections", "paper_model_choices")
        raw("Automatic Model Switching", "paper_model_switches")
        raw("Paper Flips", "paper_flips")
        if include_account_equity:
            marks = open_position_marks(db, time.time())
            accounts = []
            for account in db.execute("SELECT * FROM v2_accounts WHERE id LIKE 'adaptive-%' ORDER BY id"):
                mark = marks.get(account["id"], 0)
                equity = account["balance"] + mark if mark is not None else None
                unrealized = equity - STARTING_CASH - account["realized"] if equity is not None else None
                accounts.append([account["id"],account["budget"],STARTING_CASH,account["balance"],
                                 account["realized"],mark,equity,unrealized])
            sheets.table("Adaptive Account Equity", ["Account","Stake","Starting cash","Cash",
                         "Realized P&L","Estimated liquidation proceeds","Market equity","Unrealized P&L"], accounts)
            streak_rows = []
            ids = [r[0] for r in db.execute("SELECT id FROM v2_accounts WHERE id LIKE 'adaptive-%'")]
            for account in ids:
                pnls = [r[0] for r in db.execute("""SELECT pnl FROM v2_orders
                    WHERE account=? AND pnl IS NOT NULL ORDER BY settled,id""",(account,))]
                record = streaks(["WIN" if p>0 else "LOSS" if p<0 else "FLAT" for p in pnls],"WIN","LOSS")
                streak_rows.append([account,len(pnls),sum(p>0 for p in pnls),sum(p<0 for p in pnls),
                                   record["current"],record["length"],record["longest"]["WIN"],record["longest"]["LOSS"]])
            if "observer_rounds" in names:
                tally = observer.scoreboard(db,observer.MAIN)
                record = tally["streaks"]
                streak_rows.append([observer.MAIN,tally["trades"],tally["wins"],tally["losses"],
                                    record["current"],record["length"],record["longest"]["WIN"],record["longest"]["LOSS"]])
            sheets.table("Account Win Loss Streaks",["Account","Games played","Wins","Losses",
                        "Current result","Current length","Longest win streak","Longest loss streak"],streak_rows)
        if "hold_flip_inputs" in names:
            paired = list(hold_research.rows(db))
            sheets.table("Hold vs Flip Summary",[
                "Market condition","Group","Settled pairs","Historical or shadow flips","Hold P&L",
                "Flip-enabled P&L","Flip minus hold","Extra-tick comparable pairs",
                "Hold P&L +1 tick","Flip P&L +1 tick","Flip minus hold +1 tick"
            ], hold_research.breakdown(paired))
            sheets.table("Hold vs Flip Rounds",[
                "Market","Entry UTC","Entry model","Entry side","Entry UP probability","Entry cost","Entry fee",
                "Entry bid","Entry ask","Entry bid depth","Entry ask depth","Entry book source UTC",
                "Entry book confirmed UTC","Entry reference source UTC","Entry volatility","Entry momentum","Entry spread",
                "Absolute BTC move from open","Flip UTC","Flip model","Flip side","Flip UP probability",
                "Flip time remaining","Flip bid","Flip ask","Flip bid depth","Flip ask depth",
                "Flip book source UTC","Flip book confirmed UTC","Flip reference source UTC","Flip BTC price","Opening price",
                "Flip volatility","Flip momentum","Estimated flip improvement","Flip sale proceeds","Flip buy cost",
                "Flip sale fee","Flip buy fee","Official outcome","Hold P&L","Flip-enabled P&L","Flip minus hold",
                "Hold P&L +1 tick","Flip P&L +1 tick","Flip minus hold +1 tick","Extra-tick entry status","Extra-tick flip status","Flip classification","Actual ledger P&L"
            ], (
                [r["slug"],utc(r["time"]),r["model"],r["side"],r["probability"],r["cost"],r["fee"],
                 a.get("bid"),a.get("ask"),a.get("depth_bid"),a.get("depth_ask"),utc(a.get("book_source")),
                 utc(a.get("confirmed")),utc(a.get("reference_time")),a.get("volatility"),a.get("momentum"),
                 r["spread"],r["gap"],utc(r["flip_time"]),r["flip_model"],r["flip_side"],
                 f.get("flip_probability_up"),f.get("seconds_remaining"),f.get("bid"),f.get("ask"),
                 f.get("depth_bid"),f.get("depth_ask"),utc(f.get("book_source")),utc(f.get("confirmed")),
                 utc(f.get("reference_time")),f.get("reference"),a.get("opening"),f.get("volatility"),f.get("momentum"),
                 r["improvement"],r["credit"],r["debit"],r["sell_fee"],r["buy_fee"],r["result"],
                 r["hold_pnl"],r["flip_pnl"],r["flip_minus_hold"],r["extra_hold"],r["extra_flip"],r["extra_difference"],
                 r["entry_alt_reason"] or "NO RECORDED INPUTS",r["flip_alt_reason"],r["flip_mode"],r["pnl"]]
                for r in paired for a,f in [(r["entry_inputs"],r["flip_inputs"])]
            ))
        raw("Unfunded Adaptive Flips", "shadow_paper_flips")
        if "exit_research_entries" in names:
            sheets.table("Matched Exit Results", [
                "Entry type","Model version","Fixed entry seconds","Stake","Exit policy","Recorded entries",
                "Settled matched pairs","Shadow exits or flips","Pairs with a fresh quote","Pairs with a forecast",
                "Hold P&L","Policy P&L","Policy minus hold","Pairs improved","Pairs worsened",
            ], exit_research.summarize(exit_research.matched_rows(db)))
            sheets.table("Matched Exit Audit", [
                "Entry type","Paper order ID","Market","Entry configuration","Model version","Fixed entry seconds",
                "Stake","Entry UTC","Entry seconds after open","Close UTC","Entry side","Entry quantity",
                "Entry cost including fee","Entry fee","Exit policy","Status","Fresh quote observations",
                "Usable forecast observations","Shadow action","Action UTC","Final side","Settlement quantity",
                "Sale credit","Opposite buy debit","Verified outcome","Settlement UTC","Hold P&L","Policy P&L",
                "Policy minus hold","Entry evidence","Action evidence",
            ], ([r["source"],r["order_id"],r["slug"],r["configuration"],r["model"],r["checkpoint"],
                 r["stake"],utc(r["entry_time"]),r["entry_time"]-r["window_end"]+300,utc(r["window_end"]),
                 r["side"],r["quantity"],r["cost"],r["fee"],r["policy"],r["status"],r["eligible_quotes"],
                 r["usable_forecasts"],r["action"],utc(r["acted_at"]),r["final_side"],r["final_quantity"],
                 r["credit"],r["debit"],r["outcome"],utc(r["settled"]),r["hold_pnl"],r["pnl"],r["improvement"],
                 r["entry_details"],r["evidence"]] for r in exit_research.matched_rows(db)))
        if "model_paper_accounts" in names:
            sheets.table("Autonomous Model Accounts",
                ["Model","Starting cash","Cash","Open positions","Closed positions","Wins","Losses",
                 "Cash-outs","Realized P&L","Completed round skips","Longest win","Longest loss",
                 "Latest action","Latest reason","Latest UTC"], (
                   [r["label"],model_paper.STARTING_CASH,r["cash"],r["open"],r["closed"],r["wins"],
                    r["losses"],r["cashouts"],r["pnl"],r["skips"],r["streak"]["longest"]["WIN"],
                    r["streak"]["longest"]["LOSS"],d.get("action"),d.get("reason"),utc(d.get("time"))]
                   for r in model_paper.standings(db) for d in [r["latest"] or {}]))
            sheets.table("Autonomous Model Decisions",
                ["Family","Version","Market","Decision UTC","Seconds after open","UP estimate","Action",
                 "Reason","Book source UTC","Book confirmed UTC","Spread","Volatility","Official outcome","Brier error","Evidence"], (
                    [r["family"],r["version"],r["slug"],utc(r["time"]),r["time"]-r["start"],r["up"],r["action"],
                     r["reason"],utc(d.get("book_timestamp")),utc(d.get("book_checked")),
                     a-b if a is not None and b is not None else None,(d.get("features") or {}).get("volatility"),
                     r["outcome"],(r["up"]-int(r["outcome"]=="UP"))**2 if r["up"] is not None and r["outcome"] else None,r["evidence"]]
                    for r in db.execute("""SELECT p.*,w.start,v.outcome FROM model_paper_decisions p
                        JOIN model_paper_windows w USING(slug) LEFT JOIN verified_markets v ON v.slug=p.slug
                        ORDER BY p.id""") for d in [json.loads(r["evidence"])]
                        for a,b in [(d.get("ask",{}).get("price"),d.get("bid",{}).get("price"))]))
            sheets.table("Autonomous Model Trades",
                ["Model","Version","Market","Side","Entry UTC","Seconds after open","Quantity","Cost incl. fee",
                 "Entry fee","Status","Close UTC","Exit fee","Proceeds","Realized P&L","Official outcome",
                 "Matched hold P&L","Exit minus hold"], (
                     [r["family"],r["version"],r["slug"],r["side"],utc(r["entered"]),r["entered"]-r["start"],
                      r["quantity"],r["cost"],r["entry_fee"],r["status"],utc(r["closed"]),r["exit_fee"],
                      r["proceeds"],r["pnl"],r["outcome"],hold,r["pnl"]-hold if hold is not None else None]
                     for r in db.execute("""SELECT p.*,w.start FROM model_paper_positions p
                         JOIN model_paper_windows w USING(slug) ORDER BY p.entered""")
                     for hold in [(r["quantity"]*(r["side"]==r["outcome"])-r["cost"])
                                  if r["outcome"] and r["pnl"] is not None else None]))
        if "model_paper_timing" in names:
            timing_groups = defaultdict(lambda: [0,0,0,0,0.0,0,0.0,0.0,0])
            def timing_audit():
                for r in db.execute("""SELECT t.*,v.outcome,p.pnl AS flexible_pnl
                    FROM model_paper_timing t JOIN model_paper_windows w USING(slug)
                    LEFT JOIN verified_markets v ON v.slug=t.slug AND v.end=w.end
                    LEFT JOIN model_paper_positions p ON p.slug=t.slug AND p.family=t.family
                    ORDER BY t.time,t.slug,t.checkpoint,t.family"""):
                    pnl = (r["quantity"]*(r["side"]==r["outcome"])-r["cost"]
                           if r["outcome"] and r["quantity"] is not None else None)
                    g = timing_groups[r["family"],r["version"] or "UNAVAILABLE",r["checkpoint"]]
                    g[0] += 1
                    g[1] += r["outcome"] is not None
                    if pnl is not None:
                        g[2] += 1
                        g[3] += pnl > 0
                        g[4] += pnl
                        g[8] += pnl < 0
                        if r["flexible_pnl"] is not None:
                            g[5] += 1
                            g[6] += pnl
                            g[7] += r["flexible_pnl"]
                    yield [r["family"],r["version"],r["slug"],r["checkpoint"],utc(r["time"]),r["up"],
                           r["side"],r["quantity"],r["cost"],r["fee"],r["reason"],r["outcome"],pnl,
                           r["flexible_pnl"],pnl-r["flexible_pnl"] if pnl is not None and
                           r["flexible_pnl"] is not None else None,r["evidence"]]
            sheets.table("Autonomous Timing Audit",["Model","Version","Market","Checkpoint seconds","Observed UTC",
                "UP forecast","Side","Contracts","Cost including fees","Fee","Decision reason","Official outcome",
                "Timed hold P&L","Flexible realized P&L","Timed minus flexible","Evidence"],timing_audit())
            sheets.table("Autonomous Timing Results",["Model","Version","Checkpoint seconds","Observed rounds",
                "Verified outcomes","Settled timed fills","Wins","Losses","Flat","Skipped verified rounds",
                "Timed P&L","Matched fills","Timed matched P&L","Flexible matched P&L","Timed minus flexible"],(
                    [*key,seen,verified,fills,wins,losses,fills-wins-losses,verified-fills,pnl if fills else None,
                     paired,timed if paired else None,flexible if paired else None,
                     timed-flexible if paired else None]
                    for key,(seen,verified,fills,wins,pnl,paired,timed,flexible,losses) in sorted(timing_groups.items())))
            agreement_groups = defaultdict(lambda: [0, 0, 0, 0, 0.0])

            def agreement_audit():
                for r in model_paper.agreement_rounds(db):
                    totals = agreement_groups[r["label"]]
                    totals[0] += 1
                    if r["pnl"] is not None:
                        totals[1] += 1
                        totals[2] += r["pnl"] > 0
                        totals[3] += r["pnl"] < 0
                        totals[4] += r["pnl"]
                    yield [r["label"],r["slug"],utc(r["start"]),utc(r["end"]),r["outcome"],r["version"],
                           utc(r["entered"]),r["entered"]-r["start"] if r["entered"] is not None else None,
                           r["side"],r["cost"],r["fee"],r["pnl"]]

            sheets.table("Agreement Forward Rounds",
                         ["Strategy","Market","Open UTC","Close UTC","Official outcome","Model version",
                          "Entry UTC","Seconds after open","Side","Cost incl fees","Fee","Hold P&L"],
                         agreement_audit())
            sheets.table("Agreement Forward Results",
                         ["Strategy","Matched verified rounds","Trades","Wins","Losses","Skips","Win rate","Hold P&L"],
                         ([label,rounds,trades,wins,losses,rounds-trades,wins/(wins+losses) if wins+losses else None,
                           pnl if trades else None]
                          for label,(rounds,trades,wins,losses,pnl) in sorted(agreement_groups.items())))
            stake_groups = defaultdict(lambda: [0,0,0,0,0,0.0])
            paired = []
            def stake_observations():
                for r in db.execute("""SELECT t.*,v.outcome FROM model_paper_timing t
                    JOIN model_paper_windows w USING(slug)
                    LEFT JOIN verified_markets v ON v.slug=t.slug AND v.end=w.end
                    WHERE t.family='model_vote' AND t.checkpoint=? ORDER BY t.time""",
                    (model_paper.SIZING_CHECKPOINT,)):
                    yield "Unanimous Model Vote",r["slug"],r["time"],r["checkpoint"],r["outcome"],json.loads(r["evidence"])
                for r in db.execute("""SELECT slug,time,result,details FROM v2_orders
                    WHERE account=? ORDER BY time,id""",(hold_research.ACCOUNT,)):
                    audit = json.loads(r["details"] or "{}").get("prediction") or {}
                    yield "Adaptive model and market agree",r["slug"],r["time"],None,r["result"],audit
            def stake_audit():
                for strategy,slug,at,checkpoint,outcome,audit in stake_observations():
                    settled_fills = {}
                    for trial in audit.get("stake_comparison", []):
                        stake = trial["stake"]
                        fill = trial.get("modeled_fill") or {}
                        side = trial.get("side")
                        pnl = (fill["quantity"]*(side==outcome)-fill["total"]
                               if outcome and trial["reason"]=="SHADOW_FILL" else None)
                        g = stake_groups[strategy,stake]
                        g[0] += 1
                        g[1] += outcome is not None
                        g[2] += pnl is not None
                        g[3] += pnl is not None and pnl>0
                        g[4] += pnl is not None and pnl<0
                        if pnl is not None:
                            g[5] += pnl
                            settled_fills[stake] = pnl
                        yield [strategy,slug,utc(at),checkpoint,stake,side,trial.get("mean_probability"),
                               trial.get("edge_rate"),fill.get("quantity"),fill.get("total"),fill.get("fee"),
                               trial["reason"],outcome,pnl,utc(audit.get("book_timestamp")),
                               utc(audit.get("book_confirmed"))]
                    small,large = model_paper.SHADOW_STAKES
                    if small in settled_fills and large in settled_fills:
                        paired.append([strategy,slug,settled_fills[small],settled_fills[large],
                                       settled_fills[large]-settled_fills[small]])
            sheets.table("Shadow Stake Audit",["Strategy","Market","UTC","Checkpoint seconds","Shadow budget","Side",
                "Mean model probability","Estimated after-cost return","Modeled contracts","Modeled cost including fees",
                "Fee","Decision reason","Official outcome","Shadow P&L","Book source UTC","Book confirmed UTC"],stake_audit())
            sheets.table("Shadow Stake Results",["Strategy","Shadow budget","Observed rounds","Verified rounds","Settled shadow fills",
                "Wins","Losses","Skipped verified rounds","Net shadow P&L"],
                ([strategy,stake,seen,verified,fills,wins,losses,verified-fills,pnl if fills else None]
                 for (strategy,stake),(seen,verified,fills,wins,losses,pnl) in sorted(stake_groups.items())))
            sheets.table("Paired Shadow Stakes",["Strategy","Market","$3 shadow P&L","$9 shadow P&L","$9 minus $3"],paired)
            reference_totals = {group: [0, 0, 0, 0, 0, 0.0, 0.0, 0.0, 0, 0] for group in (
                "All audited", "UP", "DOWN", "Model and market agree",
                "Agreement and model score ≥80%", "Agreement and model score ≥90%",
                "Agreement and at least 7 of 8 votes",
            )}
            def reference_sizing():
                for r in db.execute("""SELECT p.*,d.evidence FROM model_paper_positions p
                    JOIN model_paper_decisions d ON p.family=d.family AND p.slug=d.slug
                      AND p.entered=d.time AND p.side=d.action
                    WHERE p.family=? ORDER BY p.entered,p.slug""", (model_paper.REFERENCE_GAP,)):
                    evidence = json.loads(r["evidence"])
                    trial = evidence.get("high_return_shadow")
                    if trial is None:
                        continue
                    outcome = r["outcome"]
                    fill = trial.get("modeled_fill") or {}
                    pnl_small = r["quantity"] * (r["side"] == outcome) - r["cost"] if outcome else None
                    pnl_large = (fill["quantity"] * (r["side"] == outcome) - fill["total"]
                                 if outcome and trial["reason"] == "SHADOW_FILL" else None)
                    score = trial["mean_probability"]
                    groups = ["All audited", r["side"]]
                    if trial["market_probability"] > .5 and score > .5:
                        groups.append("Model and market agree")
                        if score >= .80:
                            groups.append("Agreement and model score ≥80%")
                        if score >= .90:
                            groups.append("Agreement and model score ≥90%")
                        if (trial.get("votes_for_side") or 0) >= 7 and trial.get("missing_votes") == []:
                            groups.append("Agreement and at least 7 of 8 votes")
                    for group in groups:
                        totals = reference_totals[group]
                        totals[0] += 1
                        if pnl_small is not None:
                            totals[1] += 1
                            totals[5] += pnl_small
                            totals[8] += pnl_small > 0
                            totals[9] += pnl_small < 0
                        if pnl_large is not None:
                            totals[2] += 1
                            totals[3] += pnl_large > 0
                            totals[4] += pnl_large < 0
                            totals[6] += pnl_small
                            totals[7] += pnl_large
                    yield [r["slug"],r["version"],utc(r["entered"]),r["side"],score,trial["market_probability"],
                           trial.get("votes_for_side"),len(trial["missing_votes"]) if trial.get("missing_votes") is not None else None,
                           trial.get("edge_rate"),fill.get("quantity"),fill.get("total"),trial["reason"],outcome,
                           pnl_small,pnl_large,pnl_large-pnl_small if pnl_large is not None else None,
                           utc(evidence.get("book_timestamp")),utc(evidence.get("book_checked"))]
            sheets.table("Reference Gap 20 Audit",
                         ["Market","Model version","Entry UTC","Side","Model score","Market midpoint","Votes for side",
                          "Missing votes","Estimated $20 after-cost return","Modeled contracts","Modeled $20 cost incl fees",
                          "$20 decision reason","Official outcome","$3 Hold P&L","$20 Hold P&L","$20 less $3",
                          "Book source UTC","Book confirmed UTC"],reference_sizing())
            sheets.table("Reference Gap 20 Results",
                         ["Group","Audited entries","Official outcomes","Settled $20 fills","$20 wins","$20 losses",
                          "$20 win rate","$20 skips on settled rounds","All $3 Hold P&L","Matched $3 Hold P&L",
                          "Matched $20 Hold P&L","$20 less $3","$3 wins","$3 losses","$3 win rate"],(
                [name,seen,settled,fills,wins,losses,wins/(wins+losses) if wins+losses else None,settled-fills,
                 small if settled else None,matched if fills else None,large if fills else None,
                 large-matched if fills else None,small_wins,small_losses,
                 small_wins/(small_wins+small_losses) if small_wins+small_losses else None]
                for name,(seen,settled,fills,wins,losses,small,matched,large,small_wins,small_losses) in sorted(reference_totals.items())
            ))
        if "pooled_consensus_rounds" in names:
            pooled = pooled_consensus.summary(db)
            sheets.table("Pooled 100 Results",
                         ["Observed rounds","Verified rounds","Open positions","Settled fills","Wins","Losses",
                          "Win rate","Skipped rounds","Net P&L","Paired fills","Matched $3 P&L",
                          "Matched $100 P&L","Matched difference"], [
                [pooled["observed"],pooled["verified"],pooled["open"],pooled["settled"],
                 pooled["wins"],pooled["losses"],
                 pooled["wins"]/(pooled["wins"]+pooled["losses"]) if pooled["wins"]+pooled["losses"] else None,
                 pooled["skips"],pooled["net_pnl"],pooled["paired"],pooled["paired_baseline_pnl"],
                 pooled["paired_pooled_pnl"],
                 pooled["paired_pooled_pnl"]-pooled["paired_baseline_pnl"] if pooled["paired"] else None]
            ])
            sheets.table("Pooled 100 Round Audit",
                         ["Market","Open UTC","Close UTC","Quote evaluated UTC","Position status","Decision reason",
                          "Side","Mean model probability for side","Modeled contracts","$100 cost incl fees",
                          "$3 comparison contracts","$3 comparison cost incl fees","Official outcome",
                          "$100 hold P&L","Matched $3 hold P&L","Vote, source timestamps, and fill evidence"],
                         ([r["slug"],utc(r["start"]),utc(r["end"]),utc(r["observed"]),r["status"],r["reason"],
                           r["side"],r["probability"],r["quantity"],r["cost"],r["baseline_quantity"],r["baseline_cost"],
                           r["outcome"],r["pnl"],r["baseline_pnl"],r["evidence"]]
                          for r in db.execute(pooled_consensus.round_results() + " ORDER BY p.start,p.slug")))
        if "pooled_agreement_rounds" in names:
            sheets.table("100 Agreement Comparisons",
                         ["Rule","Observed rounds","Verified rounds","Settled fills","Wins","Losses",
                          "Win rate","Skips","Net $100 P&L","Paired fills","Matched $3 P&L","Matched $100 P&L"],
                         ([r["label"],r["observed"],r["verified"],r["settled"],r["wins"],r["losses"],
                           r["wins"]/(r["wins"]+r["losses"]) if r["wins"]+r["losses"] else None,
                           r["skips"],r["net_pnl"],r["paired"],r["paired_baseline_pnl"],r["paired_pooled_pnl"]]
                          for r in pooled_consensus.comparisons(db)))
            sheets.table("100 Agreement Round Audit",
                         ["Rule","Market","Quote evaluated UTC","Status","Reason","Side","Model probability",
                          "Modeled $100 cost","Official outcome","$100 Hold P&L","Matched $3 Hold P&L","Evidence"],
                         ([label,r["slug"],utc(r["observed"]),r["status"],r["reason"],r["side"],
                           r["probability"],r["cost"],r["outcome"],r["pnl"],r["baseline_pnl"],r["evidence"]]
                          for scenario,label in pooled_consensus.LABELS.items()
                          for r in pooled_consensus.results(db,scenario,matched=True)))
        if "selective_forecasts" in names:
            add_selective_blockers(sheets,db)
            s = selective_forecast.summary(db)
            sheets.table("Selective 90 Summary", ["Metric","Value"], [
                ["Scope","90-second forecast with a 12-basis-point opening move and a 65% heuristic score"],
                ["Observed markets",s["observed"]],["Settled calls",s["settled_calls"]],["Correct",s["right"]],
                ["Accuracy",s["right"]/s["settled_calls"] if s["settled_calls"] else None],["Skips",s["skips"]],
                ["Settled $3 shadow fills",s["settled_fills"]],["Net $3 shadow P&L",s["net_pnl"]],
                ["Archived entry win estimate",selective_forecast.HISTORICAL_PROBABILITY],
            ])
            sheets.table("Selective 90 Round Audit",
                ["Market","Open UTC","Decision UTC","Status","Forecast reason","Side","Heuristic score",
                 "Opening gap bps","Book source UTC","Book confirmed UTC","Shadow fill reason","Modeled contracts",
                 "Cost including fee","Fee","Verified outcome","Correct","Shadow hold P&L"], (
                [r["slug"],utc(r["start"]),utc(r["observed"]),r["status"],r["reason"],r["side"],r["score"],r["gap_bps"],
                 utc(r["quote_source"]),utc(r["quote_confirmed"]),r["fill_reason"],r["quantity"],r["cost"],
                 r["fee"],r["outcome"],r["correct"],r["pnl"]]
                for r in db.execute(selective_forecast.rows()+" ORDER BY p.start,p.slug")))
        if "model_market_samples" in names:
            keys = ("family","version","market","entered","closed","side","outcome","correct","up",
                    "market_up","entry_brier","market_brier","prior_entries","adjusted_up","adjusted_brier","pnl","hold","delta","entry_note","exit_note",
                    "entry_reason","exit_reason")
            sheets.table("Model Trade Review",["Model","Version","Market","Entry UTC","Close UTC","Side",
                "Official outcome","Direction correct","UP forecast","Market midpoint","Model Brier error",
                "Market Brier error","Earlier model trials in band","Adjusted UP shadow","Adjusted Brier error","Actual P&L","Matched hold P&L","Exit minus hold","Entry review",
                "Exit review","Entry trigger","Exit trigger"],(
                    [utc(r[k]) if k in ("entered","closed") else r[k] for k in keys] for r in viability.feedback(db)))
        if "agreement_cap_trials" in names:
            cap_groups = defaultdict(lambda: [0,0,0,0,0,0.0])
            def cap_audit():
                for r in viability.cap_results(db,time.time()):
                    d = json.loads(r["evidence"])
                    g = cap_groups[r["checkpoint"],r["policy"]]
                    g[0] += 1
                    if r["outcome"]:
                        g[1] += 1
                        if r["pnl"] is not None:
                            g[2] += 1
                            g[3] += r["pnl"]>0
                            g[4] += r["pnl"]<0
                            g[5] += r["pnl"]
                    yield [r["slug"],r["checkpoint"],r["policy"],utc(r["time"]),r["side"],r["up"],r["gap_bps"],
                           d.get("all_in"),d.get("spread"),d.get("volatility"),r["quantity"],r["cost"],r["fee"],
                           r["reason"],r["outcome"],r["pnl"],r["evidence"]]
            sheets.table("Agreement Price Cap Audit",["Market","Entry seconds","Policy","UTC","Side","UP estimate",
                "Reference gap bps","All-in per contract","Spread","Volatility","Quantity","Cost","Fee",
                "Decision reason","Official outcome","Hold P&L","Evidence"],cap_audit())
            sheets.table("Agreement Price Cap Results",["Entry seconds","Policy","Observed","Official outcomes",
                "Settled hypothetical fills","Wins","Losses","Settled skips","Win rate","Net hold P&L"],(
                    [*key,seen,settled,fills,wins,losses,settled-fills,wins/fills if fills else None,
                     pnl if fills else None]
                    for key,(seen,settled,fills,wins,losses,pnl) in sorted(cap_groups.items())))
        if "model_market_samples" in names:
            def sampled_conditions():
                for r in db.execute("SELECT * FROM model_market_samples ORDER BY time,slug"):
                    d=json.loads(r["evidence"])
                    yield [r["slug"],utc(r["time"]),d["spread"],d["spread_change"],d["bid_depth"],d["ask_depth"],
                           d["depth_ratio"],d["volatility"],d["volatility_ratio"],d["mid"],d["midpoint_change"],
                           d["gap_bps"],d["gap_change_bps"],d["momentum"],d["up_votes"],d["down_votes"],
                           d["missing_votes"],d["vote_margin_change"],d["book_age"],d["reference_age"],
                           d["comparison_seconds"],",".join(d["alerts"])]
            sheets.table("Market Change Audit",["Market","UTC","Spread","Spread change","Bid depth","Ask depth",
                "Depth vs prior","Volatility","Volatility vs prior","Book midpoint","Midpoint change","Reference gap bps",
                "Reference gap change bps","Momentum","UP votes","DOWN votes","Missing votes","Vote margin change",
                "Book age seconds","Reference age seconds","Seconds since prior sample","Change alerts"],sampled_conditions())
            sheets.table("Timing and Market Conditions",["Model","Version","Entry band starts at second","Market condition",
                "Closed trades","Wins","Losses","Baseline P&L","What-if skip alert P&L",
                "What-if difference"],(
                    [r["family"],r["version"],r["timing"],r["condition"],r["trades"],r["wins"],r["losses"],
                     r["pnl"],r["guarded_pnl"],r["guarded_pnl"]-r["pnl"]]
                    for r in viability.conditions(db)))
        raw("Adaptive Market Windows", "adaptive_windows")
        if {"adaptive_windows", "adaptive_decisions"} <= names:
            sheets.table("Adaptive Labeled Observations",
                         ["Market","Account","Observation UTC","Model","Estimated UP","Decision",
                          "Reason","Official outcome","Window end UTC","Audit JSON"], (
                [r["slug"],r["account"],utc(r["time"]),r["model"],r["up"],r["decision"],
                 r["reason"],r["outcome"],utc(r["end"]),r["details"]]
                for r in db.execute("""SELECT d.*,v.outcome,v.end FROM adaptive_decisions d
                    JOIN verified_markets v ON v.slug=d.slug
                    WHERE d.up IS NOT NULL AND d.time<v.end ORDER BY d.time,d.id""")
            ))
        if "adaptive_later" in names:
            sheets.table("Later Entry Comparison",
                         ["Market","Account","Side","Actual entry UTC","Actual entry seconds",
                          "Delay seconds","Target UTC","Observed UTC","Book source UTC","Later quote status",
                          "Official outcome","Early hold-only P&L","Later hold-only P&L","Later minus early","Later cost","Later fee",
                          "Later quantity","Audit JSON"], (
                [r["slug"],r["account"],r["side"],utc(r["entry"]),r["entry_seconds"],r["delay"],
                 utc(r["target"]),utc(r["time"]),utc(r["book_timestamp"]),r["reason"],r["result"],
                 r["early_pnl"],r["late_pnl"],
                 r["late_pnl"]-r["early_pnl"] if r["late_pnl"] is not None else None,
                 r["cost"],r["fee"],r["quantity"],r["details"]]
                for r in db.execute("""SELECT l.*,o.slug,o.account,o.side,o.time AS entry,
                    o.time-w.start AS entry_seconds,o.result,
                    CASE WHEN o.status='SETTLED' THEN (CASE WHEN o.side=o.result THEN o.quantity ELSE 0 END)-o.cost END AS early_pnl,
                    CASE WHEN o.status='SETTLED' AND l.quantity>0
                         THEN (CASE WHEN o.side=o.result THEN l.quantity ELSE 0 END)-l.cost END AS late_pnl
                    FROM adaptive_later l JOIN v2_orders o ON o.id=l.order_id
                    JOIN adaptive_windows w ON w.slug=o.slug ORDER BY o.time,o.id,l.delay""")
            ))
        raw("Approved Configurations", "approved_configs")
        raw("Smart Flip Research", "v2_smart_experiments")
        raw("Smart Flip Actions", "v2_smart_actions")
        raw("Lab Trade History", "lab_orders")
        raw("Lab Skipped Markets", "lab_skips")
        raw("Research Runs", "lab_runs")
        raw("Human Review", "intervention_requests")
        raw("Model Timing Predictions", "lab_predictions")
        raw("Model Forecast Readiness", "model_forecast_health")
        if "model_vote_forecasts" in names:
            def vote_rows():
                for r in db.execute("""SELECT f.*,v.outcome,o.pnl,s.reason FROM model_vote_forecasts f
                    LEFT JOIN verified_markets v ON v.slug=f.slug AND f.time<v.end
                    LEFT JOIN lab_configs c ON c.version=? AND c.checkpoint=f.checkpoint AND c.stake=1
                        AND c.exit='HOLD' AND c.id LIKE 'timing/v2/%'
                    LEFT JOIN lab_orders o ON o.config=c.id AND o.slug=f.slug
                    LEFT JOIN lab_skips s ON s.config=c.id AND s.slug=f.slug
                    ORDER BY f.slug,f.checkpoint""", (VOTE_VERSION,)):
                    yield r, json.loads(r["evidence"])
            groups = defaultdict(lambda:[0,0,0,0,0,0.0,0.0])
            def audited_votes():
                previous_slug = previous_side = None
                for r,v in vote_rows():
                    share=v["up_share"]
                    side=None if share is None or share==.5 else "UP" if share>.5 else "DOWN"
                    changed=(side!=previous_side if side and previous_side and previous_slug==r["slug"] else None)
                    previous_slug,previous_side=r["slug"],side
                    split=f"{v['up_votes']} UP / {v['down_votes']} DOWN / {v['ties']} ties"
                    outcome=r["outcome"]
                    brier=(share-int(outcome=="UP"))**2 if share is not None and outcome else None
                    volatility=v.get("volatility")
                    regime="UNKNOWN" if volatility is None else "HIGHER" if volatility>=.001 else "LOWER"
                    if share is not None:
                        groups_for_round = ["ALL"]
                        if regime != "UNKNOWN":
                            groups_for_round.append(regime+" VOLATILITY")
                        if v.get("spread") is not None:
                            groups_for_round.append("WIDE SPREAD" if v["spread"]>=.03-1e-10 else "TIGHT SPREAD")
                        if changed is not None:
                            groups_for_round.append("MAJORITY CHANGED" if changed else "MAJORITY HELD")
                        for condition in groups_for_round:
                            g=groups[r["checkpoint"],split,condition]
                            g[0]+=1
                            g[1]+=outcome is not None
                            g[2]+=side is not None and side==outcome
                            g[3]+=side is not None and outcome is not None
                            g[4]+=r["pnl"] is not None
                            g[5]+=r["pnl"] or 0
                            g[6]+=brier or 0
                    yield [r["slug"],r["checkpoint"],utc(r["time"]),split,",".join(v["missing"]),share,
                           v["mean_up"],side,changed,volatility,v.get("spread"),outcome,brier,r["pnl"],
                           r["reason"],r["evidence"]]
            sheets.table("Model Vote Audit", ["Market","Entry seconds","Forecast UTC","Vote split","Missing models",
                "UP vote share","Mean model UP probability","Majority side","Changed vs previous checkpoint",
                "Volatility","Spread","Official outcome","Vote-share Brier error","$1 fixed paper P&L","Skip reason",
                "Individual forecasts and versions"], audited_votes())
            sheets.table("Model Vote Trends", ["Entry seconds","Vote split","Market condition","Forecasts",
                "Verified outcomes","Correct majority calls","Scored majority calls","Majority accuracy",
                "Settled $1 trades","$1 net paper P&L","Mean vote-share Brier error"],
                ([*key,g[0],g[1],g[2],g[3],g[2]/g[3] if g[3] else None,g[4],g[5] if g[4] else None,
                  g[6]/g[1] if g[1] else None] for key,g in sorted(groups.items())))

        raw("Challenger Live Shadows", "model_shadows")
        if "lab_predictions" in names:
            sheets.table("Challenger Outcomes",[
                "Model family","Version","Market","Entry seconds","Predicted UTC","Estimated UP",
                "Predicted side","Official outcome","Correct","Brier error","Capture-time features"
            ], (
                [r["config_family"],r["version"],r["slug"],r["checkpoint"],utc(r["time"]),r["up"],
                 side,r["outcome"],
                 side==r["outcome"] if side and r["outcome"] else None,
                 (r["up"]-int(r["outcome"]=="UP"))**2 if r["outcome"] else None,r["features"]]
                for r in db.execute("""SELECT p.*,v.outcome FROM lab_predictions p
                    LEFT JOIN verified_markets v ON v.slug=p.slug AND p.time<v.end
                    ORDER BY p.time,p.slug,p.version,p.checkpoint""")
                for side in ["UP" if r["up"]>.5 else "DOWN" if r["up"]<.5 else None]
            ))
        sheets.table("Bug Log", ["UTC", "Severity", "Observed issue"], (
            [utc(row["time"]), row["level"], row["message"]]
            for row in db.execute("SELECT time,level,message FROM events WHERE level IN ('WARNING','ERROR') ORDER BY id")
        ))
        if {"lab_configs","lab_orders"} <= names:
            timing_rows = db.execute("""SELECT a.family,a.version,a.stake,a.checkpoint,
                count(*) AS rounds,sum(e.pnl),sum(b.pnl),sum(e.pnl-b.pnl)
                FROM lab_configs a JOIN lab_configs z
                ON z.version=a.version AND z.family=a.family AND z.stake=a.stake AND z.exit='HOLD'
                AND z.checkpoint=90 AND z.id LIKE 'timing/%'
                AND (a.id LIKE 'timing/v2/%')=(z.id LIKE 'timing/v2/%')
                JOIN lab_orders e ON e.config=a.id AND e.status='SETTLED'
                JOIN lab_orders b ON b.config=z.id AND b.slug=e.slug AND b.status='SETTLED'
                WHERE a.exit='HOLD' AND a.id LIKE 'timing/%'
                GROUP BY a.family,a.version,a.stake,a.checkpoint
                ORDER BY a.family,a.version,a.stake,a.checkpoint""")
            sheets.table("Timing vs 90s Matched", ["Model","Version","Stake","Entry seconds",
                "Matched rounds","Entry net P&L","90s net P&L","Difference"], timing_rows)
        if {"adaptive_windows", "lab_configs", "lab_orders", "lab_skips"} <= names:
            sheets.table("Adaptive vs Fixed Timing",
                         ["Fixed model","Stake","Fixed entry seconds","Comparable rounds",
                          "Adaptive entries","Fixed entries","Adaptive net P&L","Fixed net P&L",
                          "Adaptive minus fixed"], db.execute("""
                WITH paired AS (
                    SELECT c.version,c.stake,c.checkpoint,w.slug,a.id AS adaptive_order,
                           f.id AS fixed_order,coalesce(a.pnl,0) AS adaptive_pnl,coalesce(f.pnl,0) AS fixed_pnl
                    FROM adaptive_windows w JOIN verified_markets v ON v.slug=w.slug
                    JOIN lab_configs c ON c.exit='HOLD' AND c.id LIKE 'timing/v2/%'
                    LEFT JOIN v2_orders a ON a.slug=w.slug AND a.account='adaptive-'||cast(c.stake AS INTEGER)
                    LEFT JOIN v2_skips s ON s.slug=w.slug AND s.account='adaptive-'||cast(c.stake AS INTEGER)
                         AND s.checkpoint=0
                    LEFT JOIN lab_orders f ON f.slug=w.slug AND f.config=c.id
                    LEFT JOIN lab_skips fs ON fs.slug=w.slug AND fs.config=c.id
                    WHERE (a.status='SETTLED' OR (a.id IS NULL AND s.id IS NOT NULL))
                      AND (f.status='SETTLED' OR (f.id IS NULL AND fs.config IS NOT NULL))
                )
                SELECT version,stake,checkpoint,count(*),count(adaptive_order),count(fixed_order),
                       sum(adaptive_pnl),sum(fixed_pnl),sum(adaptive_pnl-fixed_pnl)
                FROM paired GROUP BY version,stake,checkpoint ORDER BY version,stake,checkpoint
                """))
        model_rows = []
        if "model_registry" in names:
            for r in db.execute("SELECT * FROM model_registry"):
                meta = json.loads(r["metadata"])
                test = meta.get("test", {})
                model_rows.append(
                    [
                        r["kind"],
                        r["version"],
                        utc(r["trained"]),
                        r["dataset"],
                        test.get("n"),
                        test.get("accuracy"),
                        test.get("brier"),
                        meta.get("split"),
                        r["sha256"],
                        "UNKNOWN",
                        meta.get("configuration"),
                    ]
                )
        sheets.table(
            "Model Comparison",
            [
                "Family",
                "Version",
                "Trained UTC",
                "Dataset SHA",
                "Test examples",
                "Accuracy",
                "Brier",
                "Split",
                "Artifact SHA",
                "Profitability",
                "Configuration",
            ],
            model_rows,
        )
        leaderboard = []
        if "lab_configs" in names:
            for config in db.execute("SELECT * FROM lab_configs").fetchall():
                opening_cash = timing_bankroll(config["id"])
                equity = peak = opening_cash
                dd = gains = losses = 0.0
                n = wins = 0
                for o in db.execute(
                    "SELECT pnl FROM lab_orders WHERE config=? AND pnl IS NOT NULL ORDER BY settled,id",
                    (config["id"],),
                ):
                    n += 1
                    wins += o["pnl"] > 0
                    equity += o["pnl"]
                    peak = max(peak, equity)
                    dd = max(dd, peak - equity)
                    gains += max(0, o["pnl"])
                    losses -= min(0, o["pnl"])
                skips = db.execute(
                    "SELECT count(*) FROM lab_skips WHERE config=?", (config["id"],)
                ).fetchone()[0]
                leaderboard.append(
                    [
                        config["version"],
                        config["checkpoint"],
                        config["exit"],
                        config["stake"],
                        equity - opening_cash if n else None,
                        (equity - opening_cash) / opening_cash if n else None,
                        n,
                        skips,
                        wins / n if n else None,
                        dd if n else None,
                        gains / losses if losses else None,
                        config["status"],
                        "OBSERVED ONLY" if n else "UNKNOWN",
                    ]
                )
        sheets.table(
            "Simulation Leaderboard",
            [
                "Version",
                "Entry seconds",
                "Exit",
                "Stake",
                "Net P&L",
                "Return",
                "Trades",
                "Skips",
                "Win rate",
                "Max drawdown USD",
                "Profit factor",
                "Scope",
                "Completeness",
            ],
            leaderboard,
        )
        sheets.table(
            "Smart Exit Comparison",
            [
                "Version",
                "Entry",
                "Exit",
                "Stake",
                "Net P&L",
                "Return",
                "Trades",
                "Skips",
                "Win rate",
                "Drawdown",
                "Profit factor",
                "Scope",
                "Completeness",
            ],
            leaderboard,
        )
        done_query = "SELECT account,slug,side,model,settled,pnl,details FROM v2_orders WHERE pnl IS NOT NULL ORDER BY settled,id"
        sheets.table(
            "WIN LOSS History",
            ["Account", "Market", "Settlement UTC", "Result", "Net P&L", "Model"],
            (
                [
                    r["account"],
                    r["slug"],
                    utc(r["settled"]),
                    "WIN" if r["pnl"] > 0 else "LOSS" if r["pnl"] < 0 else "FLAT",
                    r["pnl"],
                    r["model"],
                ]
                for r in db.execute(done_query)
            ),
        )
        daily = defaultdict(lambda: [0, 0.0, 0])
        timing = defaultdict(lambda: [0, 0.0])
        trade_values = []
        for r in db.execute(done_query):
            v = daily[(r["account"], local_day(r["settled"]))]
            v[0] += 1
            v[1] += r["pnl"]
            v[2] += r["pnl"] > 0
            checkpoint = (
                json.loads(r["details"]).get("prediction", {}).get("checkpoint", "ADAPTIVE" if r["account"].startswith("adaptive-") else "NOT AVAILABLE")
            )
            v = timing[(r["account"], str(checkpoint))]
            v[0] += 1
            v[1] += r["pnl"]
            if r["account"] == "primary":
                trade_values.append("WIN" if r["pnl"] > 0 else "LOSS" if r["pnl"] < 0 else "FLAT")
        sheets.table(
            "Daily Performance",
            ["Account", "Chicago date", "Settled trades", "Net P&L", "Win rate"],
            ([a, d, v[0], v[1], v[2] / v[0]] for (a, d), v in sorted(daily.items())),
        )
        sheets.table(
            "Entry Time Comparison",
            ["Account", "Checkpoint seconds", "Trades", "Net P&L"],
            ([a, c, v[0], v[1]] for (a, c), v in sorted(timing.items())),
        )
        outcomes = [
            r[0]
            for r in db.execute("SELECT outcome FROM verified_markets ORDER BY end DESC LIMIT 50")
        ][::-1]
        sheets.table(
            "Streak Statistics",
            ["Scope", "Statistics"],
            [
                ["Market", streaks(outcomes, "UP", "DOWN")],
                ["Primary trades", streaks(trade_values, "WIN", "LOSS")],
                ["Verified count", len(outcomes)],
            ],
        )
        primary = sheets.create("Primary Equity", ["UTC", "Equity", "Drawdown USD"])
        sheets.append(primary, ["INITIAL", 100, 0])
        curve = sheets.create(
            "Equity and Drawdown", ["UTC", "Account", "Settled equity", "Peak", "Drawdown USD"]
        )
        for a in db.execute("SELECT id FROM v2_accounts").fetchall():
            equity = peak = STARTING_CASH if a["id"].startswith("adaptive-") else 100
            sheets.append(curve, ["INITIAL", a["id"], equity, equity, 0])
            for o in db.execute(
                "SELECT pnl,settled FROM v2_orders WHERE account=? AND pnl IS NOT NULL ORDER BY settled,id",
                (a["id"],),
            ):
                equity += o["pnl"]
                peak = max(peak, equity)
                sheets.append(curve, [utc(o["settled"]), a["id"], equity, peak, peak - equity])
                if a["id"] == "primary":
                    sheets.append(primary, [utc(o["settled"]), equity, peak - equity])
        chart = LineChart()
        chart.title = "Primary settled equity (not mark-to-market)"
        chart.y_axis.title = "USD"
        chart.add_data(
            Reference(
                primary, min_col=2, max_col=2, min_row=1, max_row=sheets.counts[primary.title]
            ),
            titles_from_data=True,
        )
        dash.add_chart(chart, "D2")
    sheets.prune_empty()
    wb.save(output)


QUICK_LIMIT = 500


def write_quick_workbook(database, output):
    """Bound the export by ledger rows and leave the full audit in the existing report."""
    wb = Workbook(write_only=True)
    sheets = Sheets(wb)
    uri = Path(database).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=10)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        sheets.table("Quick Report", ["Metric", "Value"], [
            ["Generated UTC", utc(time.time())],
            ["Mode", get_state(db, "mode") or "OFF"],
            ["Scope", f"Recent paper results, newest {QUICK_LIMIT} rows per detail sheet"],
            ["Summary", "Totals use only the rows included in this report."],
            ["Pending", "Open positions and unverified outcomes have blank P&L."],
            ["Full audit", "Use Full audit workbook on the Excel export page for complete history and forecast detail."],
        ])
        add_unified(sheets,db,quick=True)
        add_corrective(sheets,db,quick=True)
        add_entry_pricing(sheets,db,quick=True)
        totals = defaultdict(lambda: [0, 0, 0, 0, 0.0])
        headers = ["Account or rule", "Market", "Entry UTC", "Side", "Status", "Outcome", "Cost incl fees", "Net P&L"]

        def ledger_rows(rows, label=lambda row: row["account"]):
            for row in rows:
                name = label(row)
                total = totals[name]
                total[0] += 1
                if row["pnl"] is not None:
                    total[1] += 1
                    total[2] += row["pnl"] > 0
                    total[3] += row["pnl"] < 0
                    total[4] += row["pnl"]
                yield [name,row["slug"],utc(row["time"]),row["side"],row["status"],
                       row["outcome"],row["cost"],row["pnl"]]

        sheets.table("Recent Paper Trades", headers, ledger_rows(db.execute("""
            SELECT account,slug,time,side,status,result AS outcome,cost,pnl FROM v2_orders
            ORDER BY id DESC LIMIT ?""", (QUICK_LIMIT,))))
        sheets.table("Recent Model Trades", headers, ledger_rows(db.execute("""
            SELECT family AS account,slug,entered AS time,side,status,outcome,cost,pnl
            FROM model_paper_positions ORDER BY rowid DESC LIMIT ?""", (QUICK_LIMIT,)),
            lambda row: model_paper.FAMILIES.get(row["account"], row["account"])))
        pooled_rows = db.execute("""
            WITH control AS (
                SELECT ? AS account,slug,end,observed AS time,side,status,cost,quantity
                FROM pooled_consensus_rounds ORDER BY rowid DESC LIMIT ?
            ), alternatives AS (
                SELECT scenario AS account,slug,end,observed AS time,side,status,cost,quantity
                FROM pooled_agreement_rounds ORDER BY rowid DESC LIMIT ?
            ), recent AS (
                SELECT * FROM control UNION ALL SELECT * FROM alternatives
            )
            SELECT p.*,v.outcome,
                   CASE WHEN v.outcome IS NOT NULL AND p.status='FILLED'
                        THEN (p.side=v.outcome)*p.quantity-p.cost END AS pnl
            FROM recent p LEFT JOIN verified_markets v ON v.slug=p.slug AND v.end=p.end
            ORDER BY time DESC,p.slug DESC,account LIMIT ?""",
            (pooled_consensus.CONTROL, QUICK_LIMIT, QUICK_LIMIT, QUICK_LIMIT))
        sheets.table("Recent Pooled Research", headers, ledger_rows(pooled_rows, lambda row:
            pooled_consensus.LABELS.get(row["account"], row["account"])))
        sheets.table("Recent Skips", ["Account", "Market", "UTC", "Reason"],
            ([r["account"],r["slug"],utc(r["time"]),r["reason"]] for r in db.execute("""
             SELECT account,slug,time,reason FROM v2_skips ORDER BY id DESC LIMIT ?""", (QUICK_LIMIT,))))
        add_selective_blockers(sheets,db)
        s = selective_forecast.summary(db)
        sheets.table("Selective 90 Summary", ["Metric","Value"], [
            ["Observed markets",s["observed"]],["Settled calls",s["settled_calls"]],["Correct",s["right"]],
            ["Accuracy",s["right"]/s["settled_calls"] if s["settled_calls"] else None],["Skips",s["skips"]],
            ["Settled $3 shadow fills",s["settled_fills"]],["Net $3 shadow P&L",s["net_pnl"]],
        ])
        sheets.table("Recent Performance", ["Account or rule","Records","Settled fills","Wins","Losses","Net P&L"],
            ([name,records,settled,wins,losses,pnl if settled else None]
             for name,(records,settled,wins,losses,pnl) in sorted(totals.items())))
    wb.save(output)


if __name__ == "__main__":
    try:
        os.nice(5)
    except (AttributeError, OSError):
        pass
    writer = write_quick_workbook if sys.argv[3:] == ["quick"] else write_workbook
    writer(sys.argv[1], sys.argv[2])
