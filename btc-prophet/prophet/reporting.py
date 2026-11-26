"""Excel audit report from one consistent SQLite snapshot; no invented results."""

import io
import json
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone

from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, Reference
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter


def utc(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if t else ""


def safe(v):
    if isinstance(v, (dict, list)):
        v = json.dumps(v)
    return "'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@") else v


def table(wb, name, headers, rows):
    ws = wb.create_sheet(name)
    ws.append(headers)
    for row in rows:
        ws.append([safe(v) for v in row])
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for c in ws[1]:
        c.font = Font(color="FFFFFF", bold=True)
        c.fill = PatternFill("solid", fgColor="17243A")
    for i, col in enumerate(ws.columns, 1):
        ws.column_dimensions[get_column_letter(i)].width = min(
            55, max(14, max(len(str(c.value or "")) for c in list(col)[:100]) + 2)
        )
    for row in ws.iter_rows(min_row=2):
        for c in row:
            if isinstance(c.value, float):
                c.number_format = "0.0000;[Red]-0.0000"
    return ws


def drawdown(orders):
    equity = peak = 100.0
    dd = 0.0
    for o in sorted(orders, key=lambda o: o["settled"] or 0):
        if o["pnl"] is None:
            continue
        equity += o["pnl"]
        peak = max(peak, equity)
        dd = max(dd, (peak - equity) / peak)
    return dd


def workbook(store, paper=None):
    with store.connect() as db:
        db.execute("BEGIN")
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        data = {
            name: [dict(r) for r in db.execute("SELECT * FROM " + name)] if name in names else []
            for name in [
                "accounts",
                "orders",
                "skips",
                "predictions",
                "risk_events",
                "equity",
                "model_runs",
                "smart_experiments",
            ]
        }
        r = db.execute("SELECT payload FROM state WHERE key='experiment'").fetchone()
        experiment = json.loads(r[0]) if r else {}
    accounts = data["accounts"]
    orders = data["orders"]
    wb = Workbook()
    wb.remove(wb.active)
    dashboard = table(
        wb,
        "Performance",
        ["Metric", "Value", "Interpretation"],
        [
            ["Report", "PAPER AND SHADOW ONLY", "No real-money orders"],
            ["Generated UTC", utc(datetime.now(timezone.utc).timestamp()), ""],
            ["Experiment", experiment.get("status", "NOT STARTED"), "Seven actual days required"],
            ["Start UTC", utc(experiment.get("start")), ""],
            ["End UTC", utc(experiment.get("end")), "Wall clock; outages remain in risk history"],
            [
                "Execution",
                "25% observed depth; one adverse tick",
                "Simulated IOC orders, not guaranteed real fills",
            ],
            ["Fee model", "Theta >= 0.095; no rebates", "Banker-rounded cumulative fee cap"],
            ["Equity convention", "Settled P&L / cost basis", "Unresolved positions are not wins"],
            [
                "Hourly control",
                "Independent accounts without the hourly skip",
                "Risk shutdowns may cause additional divergence",
            ],
        ],
    )
    by_account = defaultdict(list)
    for o in orders:
        by_account[o["account"]].append(o)
    comparisons = []
    for a in accounts:
        done = [o for o in by_account[a["id"]] if o["pnl"] is not None]
        exposure = sum(o["cost"] for o in by_account[a["id"]] if o["status"] == "OPEN")
        comparisons.append(
            [
                a["id"],
                a["budget"],
                100,
                a["balance"],
                exposure,
                a["balance"] + exposure,
                a["realized"],
                len(done),
                sum(o["pnl"] > 0 for o in done) / len(done) if done else None,
                drawdown(done),
                a["streak"],
                a["halted"],
            ]
        )
    strategies = table(
        wb,
        "Strategy Comparisons",
        [
            "Account",
            "Fixed budget incl fees",
            "Initial funding",
            "Cash",
            "Open cost basis",
            "Equity at cost",
            "Settled net P&L",
            "Settled trades",
            "Win rate",
            "Max drawdown",
            "Loss streak since review",
            "Shutdown",
        ],
        comparisons,
    )
    for row in strategies.iter_rows(min_row=2):
        row[8].number_format = row[9].number_format = "0.00%"
    trade_rows = []
    timing = defaultdict(list)
    volatility = defaultdict(list)
    for o in orders:
        d = json.loads(o["details"])
        p = d["prediction"]
        trade_rows.append(
            [
                o["id"],
                utc(o["time"]),
                o["account"],
                o["slug"],
                o["side"],
                o["probability"],
                o["quantity"],
                o["cost"],
                o["fee"],
                o["status"],
                o["result"],
                o["pnl"],
                o["model"],
                utc(o["settled"]),
                p.get("elapsed"),
                p.get("volatility"),
                d["fill"]["status"],
                o["details"],
            ]
        )
        timing[int(p.get("elapsed", 0)) // 10 * 10].append(o)
        v = p.get("volatility", 0)
        volatility["LOW" if v < 0.0001 else "MEDIUM" if v < 0.0003 else "HIGH"].append(o)
    trades = table(
        wb,
        "Trade History",
        [
            "ID",
            "Entry UTC",
            "Account",
            "Market",
            "Side",
            "Estimated UP",
            "Quantity",
            "Total cost incl fees",
            "Entry fee",
            "Status",
            "Official outcome",
            "Net P&L",
            "Model",
            "Settled UTC",
            "Entry elapsed seconds",
            "Volatility",
            "Fill status",
            "Audit JSON",
            "P&L audit formula",
        ],
        trade_rows,
    )
    for r in range(2, len(trade_rows) + 2):
        trades.cell(r, 19, f'=IF(J{r}="SETTLED",IF(E{r}=K{r},G{r},0)-H{r},"")')
    table(
        wb,
        "Skipped Trades",
        ["UTC", "Account", "Market", "Reason"],
        [[utc(s["time"]), s["account"], s["slug"], s["reason"]] for s in data["skips"]],
    )

    def grouped(groups):
        result = []
        for key, items in sorted(groups.items()):
            done = [o for o in items if o["pnl"] is not None]
            result.append(
                [
                    key,
                    len(items),
                    len(done),
                    sum(o["pnl"] for o in done),
                    sum(o["pnl"] > 0 for o in done) / len(done) if done else None,
                ]
            )
        return result

    table(
        wb,
        "Entry Time Analysis",
        ["Seconds after open (10s bin)", "Entries", "Settled", "Net P&L", "Win rate"],
        grouped(timing),
    )
    table(
        wb,
        "Volatility Analysis",
        ["15s volatility regime", "Entries", "Settled", "Net P&L", "Win rate"],
        grouped(volatility),
    )
    for sheet, key in [
        ("Model Learning", "model_runs"),
        ("Smart Exit and Flip", "smart_experiments"),
    ]:
        table(wb, sheet, ["Recorded ID", "Full audit JSON"], [[r.get("id"), r] for r in data[key]])
    table(
        wb,
        "Risk Events",
        ["UTC", "Account", "Reason", "Reviewed UTC"],
        [
            [utc(r["time"]), r["account"], r["reason"], utc(r["reviewed"])]
            for r in data["risk_events"]
        ],
    )
    table(
        wb,
        "Predictions",
        [
            "Market",
            "UTC",
            "Model",
            "Estimated UP",
            "Decision",
            "Reason",
            "Hourly checkpoint",
            "Feature audit",
        ],
        [
            [
                p["slug"],
                utc(p["time"]),
                p["model"],
                p["up"],
                p["decision"],
                p["reason"],
                p["checkpoint"],
                p["details"],
            ]
            for p in data["predictions"]
        ],
    )
    table(
        wb,
        "Hourly Checkpoint",
        ["Budget", "Main P&L", "Control P&L", "Main minus control"],
        [
            [
                b,
                next(a["realized"] for a in accounts if a["id"] == f"main-{b}"),
                next(a["realized"] for a in accounts if a["id"] == f"control-{b}"),
                next(a["realized"] for a in accounts if a["id"] == f"main-{b}")
                - next(a["realized"] for a in accounts if a["id"] == f"control-{b}"),
            ]
            for b in (1, 2, 3)
        ],
    )
    curve = table(wb, "Equity Curve", ["Settlement number"] + [a["id"] for a in accounts], [])
    values = {
        a["id"]: [100] + [100 + e["realized"] for e in data["equity"] if e["account"] == a["id"]]
        for a in accounts
    }
    for i in range(max((len(v) for v in values.values()), default=1)):
        curve.append(
            [i] + [values[a["id"]][i] if i < len(values[a["id"]]) else None for a in accounts]
        )
    chart = LineChart()
    chart.title = "Settled equity by account (initial funding $100)"
    chart.y_axis.title = "USD"
    chart.add_data(
        Reference(curve, min_col=2, max_col=len(accounts) + 1, min_row=1, max_row=curve.max_row),
        titles_from_data=True,
    )
    dashboard.add_chart(chart, "A13")
    bars = BarChart()
    bars.title = "Settled P&L by virtual account"
    bars.add_data(
        Reference(strategies, min_col=7, min_row=1, max_row=len(accounts) + 1),
        titles_from_data=True,
    )
    bars.set_categories(Reference(strategies, min_col=1, min_row=2, max_row=len(accounts) + 1))
    dashboard.add_chart(bars, "J13")
    try:
        historical = json.loads((Path(store.path).parent / "backtest14/report.json").read_text())
    except (OSError, ValueError):
        historical = None
    if historical:
        if historical.get("status", "").startswith("WITHDRAWN"):
            table(
                wb,
                "Historical Validation",
                ["Status", "Reason"],
                [[historical["status"], historical.get("withdrawal_reason")]],
            )
            historical = None
    if historical:
        table(
            wb,
            "Historical Scenarios",
            [
                "CONDITIONAL FILL ASSUMPTIONS",
                "Account",
                "Net P&L",
                "Trades",
                "Win rate",
                "Halted",
                "Assumed capacity",
                "Exploratory after inspection",
            ],
            [
                [
                    sc.get("label", f"Capacity {sc['capacity_fraction']}"),
                    a["id"],
                    a["realized"],
                    a["trades"],
                    a["wins"] / a["trades"] if a["trades"] else None,
                    a["halted"],
                    sc["capacity_fraction"],
                    sc.get("exploratory_after_test_inspection", False),
                ]
                for sc in historical["scenarios"]
                for a in sc["accounts"]
            ],
        )
        table(wb, "Historical Caveats", ["Limitation"], [[v] for v in historical["limitations"]])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
