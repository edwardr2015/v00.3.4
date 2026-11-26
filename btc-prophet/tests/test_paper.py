from datetime import datetime, timezone

import pytest

from prophet.execution import execute, fee
from prophet.paper import MODEL, PaperEngine
from prophet.risk import entry_block
from prophet.store import Store

START = 1800000000


def seed(tmp_path, checkpoint=False):
    store = Store(str(tmp_path / "paper.sqlite"))
    engine = PaperEngine(store)
    start = START if checkpoint else START + 300
    now = start + 90

    def iso(t):
        return datetime.fromtimestamp(t, timezone.utc).isoformat()

    market = dict(
        slug="synthetic-test-market",
        active=True,
        closed=False,
        status="MARKET_STATUS_OPEN",
        orderPriceMinTickSize=0.01,
        minimumTradeQty=0.01,
        feeCoefficient=0.095,
        assetPriceTerms=dict(
            asset={"symbol": "btc"},
            marketType="ASSET_PRICE_MARKET_TYPE_UP_DOWN",
            indexSymbol="BRTI",
            windowStart=iso(start),
            windowEnd=iso(start + 300),
            priceToBeat={"value": "100"},
        ),
    )
    store.put("market", market)
    store.put(
        "book",
        dict(
            slug=market["slug"],
            timestamp=now,
            state="MARKET_STATE_OPEN",
            bids=[dict(price=0.24, quantity=1000)],
            asks=[dict(price=0.25, quantity=1000)],
        ),
    )
    store.put("reference", dict(timestamp=now, price=110))
    for name in ("reference", "market"):
        store.put("health_" + name, dict(ok=True, checked=now, error=None))
    store.save_candles(
        [
            dict(
                time=t,
                open=100 + (t - (start - 225)) * 0.01,
                close=100 + (t - (start - 225)) * 0.01,
                high=100 + (t - (start - 225)) * 0.01,
                low=100 + (t - (start - 225)) * 0.01,
                ticks=1,
            )
            for t in range(start - 225, start + 90)
        ]
    )
    return store, engine, now


def test_execution_budget_fee_depth_and_no_liquidity():
    book = dict(bids=[dict(price=0.4, quantity=1)], asks=[dict(price=0.5, quantity=1)])
    fill = execute(book, "UP", budget=1)
    assert fill["quantity"] == 0.25 and fill["status"] == "PARTIAL"
    assert fill["total"] <= 1 and fill["average_price"] == 0.51
    assert fill["fee"] >= 0
    down = execute(book, "DOWN", budget=1)
    assert down["average_price"] == 0.61
    assert execute(dict(bids=[], asks=[]), "UP", budget=1)["status"] == "UNFILLED"
    assert fee(0.025) == 0.02 and fee(0.035) == 0.04


def test_entries_are_idempotent_and_settlement_has_no_double_credit(tmp_path):
    s, e, now = seed(tmp_path)
    e.mode("SIMULATE", now)
    e.tick(now)
    e.tick(now)
    summary = e.summary()
    assert len(summary["orders"]) == 6
    for a in summary["accounts"]:
        assert 0 < 100 - a["balance"] <= a["budget"]
        assert a["exposure"] <= a["budget"]
    s.put(
        "outcome_synthetic-test-market",
        dict(slug="synthetic-test-market", outcome="UP", yes_payout=1),
    )
    e.settle(now)  # Cannot settle before the window ends.
    assert all(o["status"] == "OPEN" for o in e.summary()["orders"])
    e.settle(now + 300)
    first = e.summary()
    e.settle(now + 300)
    assert e.summary()["accounts"] == first["accounts"]
    assert all(a["realized"] > 0 and a["streak"] == 0 for a in first["accounts"])


def test_hourly_checkpoint_main_skips_control_trades(tmp_path):
    s, e, now = seed(tmp_path, True)
    e.mode("SIMULATE", now)
    e.tick(now)
    assert len(e.summary()["orders"]) == 3
    assert all(o["account"].startswith("control") for o in e.summary()["orders"])
    with s.connect() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM skips WHERE reason='HOURLY_OBSERVATION_CHECKPOINT'"
            ).fetchone()[0]
            == 3
        )


def test_stale_data_latches_and_collection_state_is_not_disabled(tmp_path):
    s, e, now = seed(tmp_path)
    e.mode("SIMULATE", now)
    e.tick(now + 20)
    assert s.get("mode") == "OFF"
    assert all(a["halted"] == "STALE_REFERENCE" for a in e.summary()["accounts"])
    with pytest.raises(ValueError, match="Review"):
        e.mode("SIMULATE", now)
    e.review()
    assert all(a["halted"] is None for a in e.summary()["accounts"])
    assert not e.summary()["orders"]


@pytest.mark.parametrize(
    "updates,reason",
    [
        ({"streak": 5}, "FIVE_CONSECUTIVE_LOSSES"),
        ({"day_pnl": -10}, "DAILY_LOSS_LIMIT"),
        ({"realized": -20}, "TOTAL_LOSS_LIMIT"),
        ({"day_pnl": -9}, "DAILY_LOSS_RESERVE"),
        ({"realized": -19}, "TOTAL_LOSS_RESERVE"),
        ({"halted": "STOP"}, "MANUAL_REVIEW_REQUIRED"),
    ],
)
def test_hard_limits_cannot_be_overridden_by_prediction(updates, reason):
    a = dict(streak=0, day="2027-01-15", day_pnl=0, realized=0, balance=100, halted=None)
    from prophet.risk import utc_day

    a["day"] = utc_day(START)
    a.update(updates)
    assert entry_block(a, 0, 2, START) == reason


def test_model_freeze_and_loss_limit_review_does_not_refund_accounts(tmp_path):
    s, e, now = seed(tmp_path)
    e.mode("SIMULATE", now)
    assert s.get("experiment")["model"] == MODEL
    with s.connect() as db:
        db.execute("UPDATE accounts SET realized=-20,balance=80,halted='TOTAL_LOSS_LIMIT'")
    e.review()
    assert all(a["balance"] == 80 and a["halted"] for a in e.summary()["accounts"])
    with pytest.raises(ValueError):
        e.mode("LIVE", now)
    assert not e.summary()["orders"]
