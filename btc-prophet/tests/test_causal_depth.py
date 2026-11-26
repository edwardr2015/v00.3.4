import json
import sqlite3

import pytest

from prophet.backtest import replay
from prophet.depth import at, initialize, validate_row
from test_reporting_backtest import historical_rows


def book(source, received):
    return validate_row(
        dict(
            market_slug="synthetic-0",
            event_type="marketData",
            timestamp=source,
            local_timestamp=received,
            bid_prices=[0.51],
            bid_sizes=[1000],
            offer_prices=[0.52],
            offer_sizes=[1000],
            state="MARKET_STATE_OPEN",
        ),
        "synthetic-0",
    )


def test_future_received_or_future_source_snapshot_is_not_available():
    db = sqlite3.connect(":memory:")
    initialize(db)
    t = 1800000360 * 10**9
    for source, received in [(t - 10**9, t + 1), (t + 1, t - 1)]:
        b = book(source, received)
        db.execute(
            "INSERT INTO historical_depth VALUES (?,?,?,?,?)",
            ("synthetic-0", source, received, json.dumps(b), "fixture"),
        )
    assert at(db, "synthetic-0", t) is None
    b = book(t - 10**9, t - 1)
    db.execute(
        "INSERT INTO historical_depth VALUES (?,?,?,?,?)",
        ("synthetic-0", t - 10**9, t - 1, json.dumps(b), "fixture"),
    )
    assert at(db, "synthetic-0", t)["received_ns"] <= t
    assert at(db, "synthetic-0", t + 20 * 10**9) is None


def test_regression_minute_price_does_not_create_a_fake_lottery_win():
    rows = historical_rows(1)
    r = rows[0]
    entry = r["decision_time"] * 10**9
    r["up_display_ask"] = 0.90
    r["down_display_ask"] = 0.11  # Conflicting interval-labelled history.
    r["observed_book"] = book(entry - 100_000_000, entry - 50_000_000)
    legacy = replay(rows, lambda x: 0.4696923321, checkpoint=False)
    corrected = replay(rows, lambda x: 0.4696923321, checkpoint=False, use_depth=True)
    assert legacy["accounts"][0]["trades"] == 1
    assert corrected["accounts"][0]["trades"] == 0
    assert all(r["quote_source"] == "CAUSAL_L2" for r in corrected["ledger"])


def test_replay_rechecks_causality_even_if_adapter_is_bypassed():
    rows = historical_rows(1)
    entry = rows[0]["decision_time"] * 10**9
    rows[0]["observed_book"] = book(entry - 1, entry + 1)
    assert all(a["trades"] == 0 for a in replay(rows, lambda x: 0.9, use_depth=True)["accounts"])


def test_wrong_venue_shape_and_inconsistent_depth_rejected():
    with pytest.raises(ValueError):
        validate_row(dict(market_slug="international", event_type="book"), "synthetic-0")
    b = dict(
        market_slug="synthetic-0",
        event_type="marketData",
        timestamp=1800000000 * 10**9,
        local_timestamp=1800000000 * 10**9,
        bid_prices=[0.6],
        bid_sizes=[1],
        offer_prices=[0.5],
        offer_sizes=[1],
        state="MARKET_STATE_OPEN",
    )
    with pytest.raises(ValueError, match="Crossed"):
        validate_row(b, "synthetic-0")
