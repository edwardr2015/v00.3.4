import io

from openpyxl import load_workbook

from prophet.backtest import assumed_fill, replay
from prophet.reporting import safe, workbook
from test_paper import seed


def historical_rows(count=15):
    return [
        dict(
            slug=f"synthetic-{i}",
            decision_time=1800000360 + i * 300,
            label_end=1800000600 + i * 300,
            quote_age=0,
            up_display_ask=0.2,
            down_display_ask=0.81,
            x=[1, 1, -5],
            y=0,
            market_terms=dict(
                orderPriceMinTickSize=0.01, minimumTradeQty=0.01, feeCoefficient=0.095
            ),
        )
        for i in range(count)
    ]


def test_scenarios_are_conditional_and_risk_stops_are_not_silently_reset():
    rows = historical_rows()
    r = replay(rows, lambda x: 0.9, checkpoint=False)
    a = next(a for a in r["accounts"] if a["id"] == "main-1")
    assert a["trades"] == 5 and a["halted"] == "FIVE_CONSECUTIVE_LOSSES"
    assert a["balance"] >= 95
    assert any(r["reason"] == "MANUAL_REVIEW_REQUIRED" for r in r["ledger"])
    for capacity in (0, 0.5, 1):
        result = replay(rows, lambda x: 0.9, capacity, checkpoint=False)
        assert all(a["balance"] >= 80 for a in result["accounts"])
        assert all(r["cost"] <= int(r["account"][-1]) * capacity + 1e-9 for r in result["ledger"])
    zero = replay(rows, lambda x: 0.9, 0, checkpoint=False)
    assert all(a["balance"] == 100 and a["trades"] == 0 for a in zero["accounts"])


def test_stale_or_missing_market_terms_do_not_produce_historical_fills():
    rows = historical_rows(2)
    rows[0]["quote_age"] = 30
    rows[1]["market_terms"] = {}
    r = replay(rows, lambda x: 0.9)
    assert all(a["trades"] == 0 for a in r["accounts"])
    assert assumed_fill(0.99, 1, 1)["quantity"] == 0


def test_excel_empty_accounts_are_not_fabricated_results(tmp_path):
    s, p, now = seed(tmp_path)
    b = load_workbook(io.BytesIO(workbook(s, p)))
    expected = {
        "Performance",
        "Trade History",
        "Skipped Trades",
        "Strategy Comparisons",
        "Entry Time Analysis",
        "Volatility Analysis",
        "Model Learning",
        "Smart Exit and Flip",
        "Risk Events",
        "Equity Curve",
    }
    assert expected.issubset(b.sheetnames)
    assert b["Trade History"].max_row == 1
    assert b["Strategy Comparisons"]["I2"].value is None
    assert len(b["Performance"]._charts) == 2
    assert b["Equity Curve"]["B2"].value == 100
    assert safe('=WEBSERVICE("bad")').startswith("'")


def test_excel_settled_pnl_reconciles_to_account_balances(tmp_path):
    s, p, now = seed(tmp_path)
    p.mode("SIMULATE", now)
    p.tick(now)
    s.put(
        "outcome_synthetic-test-market",
        dict(slug="synthetic-test-market", outcome="DOWN", yes_payout=0),
    )
    p.settle(now + 300)
    b = load_workbook(io.BytesIO(workbook(s, p)))
    sheet = b["Strategy Comparisons"]
    for row in sheet.iter_rows(min_row=2, values_only=True):
        assert abs(row[3] - 100 - row[6]) < 1e-9
    trade = b["Trade History"]
    assert trade.max_row == 7 and trade["S2"].value.startswith("=IF(")
    assert sum(trade.cell(r, 12).value for r in range(2, 8)) < 0
