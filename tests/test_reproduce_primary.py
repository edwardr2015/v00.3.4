import sys
from pathlib import Path

import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import reproduce_primary as rp  # noqa: E402

HEAD = ["Market", "Entry UTC", "Side", "Model", "UP estimate", "Budget", "Quantity",
        "Cost incl. fees", "Entry fee", "Status", "Exit UTC", "Proceeds", "Paper P&L",
        "Official outcome"]


def book(tmp_path, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Unified Trades"
    ws.append(HEAD)
    for r in rows:
        ws.append(r)
    p = tmp_path / "r.xlsx"
    wb.save(p)
    return p


def test_hold_confidence_and_timing(tmp_path):
    p = book(tmp_path, [
        # sold early at a loss, but would have won at settlement
        ["cpc-btc-updown-5m-2026-11-26-1355z", "2026-11-26 13:56:26", "DOWN", "m", 0.25, 5, 8.0,
         4.0, 0.2, "SOLD", "", 3.0, -1.0, "DOWN"],
        # held, lost; entered at +120 s, confidence 0.65
        ["cpc-btc-updown-5m-2026-11-26-1400z", "2026-11-26 14:02:00", "UP", "m", 0.65, 5, 10.0,
         5.0, 0.2, "SETTLED", "", 0, -5.0, "DOWN"],
        # pending rows are ignored
        ["cpc-btc-updown-5m-2026-11-26-1405z", "2026-11-26 14:05:10", "UP", "m", 0.9, 5, 10.0,
         5.0, 0.2, "OPEN", "", None, None, None],
    ])
    rows = rp.load(p)
    assert len(rows) == 2
    assert rows[0]["hold"] == 4.0 and rows[0]["confidence"] == 0.75 and rows[0]["second"] == 86
    assert rows[1]["second"] == 120
    res = {r["cohort"]: r for r in rp.reproduce(rows)}
    assert res["Existing strategy (actual)"]["net_pnl"] == -6.0
    assert res["Hold to settlement (same entries)"]["net_pnl"] == -1.0
    assert res["Hold to settlement (same entries)"]["wins"] == 1
    assert res["Confidence >= 70%"]["trades"] == 1
    assert res["Entry < 90 s"]["trades"] == 1 and res["Entry >= 90 s"]["trades"] == 1
    assert res["Existing strategy (actual)"]["max_drawdown"] == 6.0
