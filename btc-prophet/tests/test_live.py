"""2.1 live trader, against a fake exchange: no real orders are ever sent by these tests."""
import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from prophet import live
from prophet.app import create_app
from prophet.store import Store

H = {"X-Prophet-Control": "local-dashboard"}


class FakeOrders:
    def __init__(self, fail=False, fill=True):
        self.sent, self.previews, self.fail, self.fill = [], [], fail, fill

    def create(self, req):
        self.sent.append(req)
        if self.fail:
            raise RuntimeError("timeout")
        if not self.fill:
            return dict(id="x", executions=[])
        px = float(req["price"]["value"])
        if req["intent"].startswith("ORDER_INTENT_SELL"):
            px = max(px, .30)
        return dict(id="o%d" % len(self.sent), executions=[dict(type="EXECUTION_TYPE_FILL", lastShares=str(req["quantity"]),
                    lastPx=dict(value=str(px), currency="USD"), commissionNotionalCollected=dict(value="0.08", currency="USD"))])

    def preview(self, params):
        self.previews.append(params)
        return dict(order=dict(state="ORDER_STATE_NEW"))


class FakeAccount:
    def __init__(self, power=50):
        self.power = power

    def balances(self):
        return dict(balances=[dict(currentBalance=self.power, buyingPower=self.power)])


class FakeClient:
    def __init__(self, **kw):
        self.orders, self.account = FakeOrders(**{k: v for k, v in kw.items() if k in ("fail", "fill")}), FakeAccount(kw.get("power", 50))


def setup(tmp_path, mode="LIVE", **kw):
    store = Store(str(tmp_path / "prophet.sqlite3"))
    create_app(store.path, collect=False, auth_required=False)
    (tmp_path / "live-keys.json").write_text(json.dumps(dict(key_id="k", secret_key="s")))
    (tmp_path / "live-convention.json").write_text(json.dumps(dict(short_price="down")))
    (tmp_path / "live-mode").write_text(mode)
    trader = live.LiveTrader(store)
    trader.client = FakeClient(**kw)
    return store, trader


def market(store, now, slug="m1", side="UP", status="OPEN", bid=.55, ask=.57):
    store.put("book", dict(slug=slug, bids=[dict(price=bid, quantity=500)], asks=[dict(price=ask, quantity=500)], timestamp=now))
    store.put("health_market", dict(ok=True, checked=now))
    with store.connect() as db:
        from prophet import selective50
        selective50.initialize(db)
        db.execute("INSERT OR REPLACE INTO selective50_trades(slug,time,end,side,quantity,cost,status,details) VALUES (?,?,?,?,5,2.9,?,'{}')",
                   (slug, now - 1, now + 100, side, status))


def run(trader, now):
    asyncio.run(trader.step(now))


def test_off_by_default_and_needs_keys_and_convention(tmp_path):
    store = Store(str(tmp_path / "p.sqlite3"))
    assert live.mode(store) == "OFF"
    with pytest.raises(ValueError):
        live.set_mode(store, "preview", "t")                       # no keys yet
    (tmp_path / "live-keys.json").write_text(json.dumps(dict(key_id="k", secret_key="s")))
    assert live.set_mode(store, "preview", "t") == "PREVIEW"
    with pytest.raises(ValueError):
        live.set_mode(store, "live", "t")                          # test order step not done


def test_live_buys_once_with_3_dollar_cap_and_correct_intent(tmp_path):
    store, trader = setup(tmp_path)
    now = time.time()
    market(store, now, side="DOWN")                                # DOWN: buy short at 1-.55 = 45c
    run(trader, now)
    run(trader, now + 1)                                           # second pass: no duplicate order
    sent = trader.client.orders.sent
    assert len(sent) == 1 and sent[0]["intent"] == "ORDER_INTENT_BUY_SHORT"
    assert sent[0]["tif"] == "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL" and sent[0]["price"]["value"] == "0.46"
    assert sent[0]["quantity"] * .46 <= 3
    with store.connect() as db:
        r = db.execute("SELECT * FROM live_orders").fetchone()
    assert r["state"] == "FILLED" and r["cost"] + r["fees"] <= 3.1


def test_error_is_never_retried_and_switches_live_off(tmp_path):
    store, trader = setup(tmp_path, fail=True)
    now = time.time()
    market(store, now)
    run(trader, now)
    run(trader, now + 1)
    assert len(trader.client.orders.sent) == 1 and live.mode(store) == "OFF"


def test_preview_mode_sends_no_orders(tmp_path):
    store, trader = setup(tmp_path, mode="PREVIEW")
    now = time.time()
    market(store, now)
    run(trader, now)
    assert not trader.client.orders.sent and len(trader.client.orders.previews) == 1


def test_guards_stale_book_price_and_exchange_buying_power(tmp_path):
    store, trader = setup(tmp_path)
    now = time.time()
    market(store, now, ask=.93)                                    # above 90c
    run(trader, now)
    market(store, now, slug="m2")
    store.put("health_market", dict(ok=True, checked=now - 10))    # stale book
    run(trader, now)
    with store.connect() as db:
        states = dict(db.execute("SELECT slug, state FROM live_orders").fetchall())
    assert "PRICE_ABOVE_90C" in states["m1"] and "STALE_BOOK" in states["m2"]
    store2, trader2 = setup(tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path, power=1)
    market(store2, now, slug="m3")
    run(trader2, now)
    assert not trader2.client.orders.sent                           # exchange has less than $3


def test_bankroll_never_below_zero_and_daily_loss_stop(tmp_path):
    store, trader = setup(tmp_path)
    with store.connect() as db:
        for i in range(4):                                         # four settled losers today: -$12
            db.execute("""INSERT INTO live_orders(slug,time,mode,side,limit_price,quantity,request,state,filled,cost,fees,pnl,end)
                VALUES (?,?,'LIVE','UP',.5,6,'{}','SETTLED',6,2.92,.08,-3,?)""", (f"old{i}", time.time() - 60, time.time()))
    now = time.time()
    market(store, now)
    run(trader, now)
    assert not trader.client.orders.sent
    with store.connect() as db:
        assert "DAILY_LOSS_LIMIT" in db.execute("SELECT state FROM live_orders WHERE slug='m1'").fetchone()[0]
        assert live.ledger(db)["cash"] == 50 - 12


def test_reversal_exit_sells_once_and_settles_pnl(tmp_path):
    store, trader = setup(tmp_path)
    now = time.time()
    market(store, now, side="UP")
    run(trader, now)                                               # bought UP
    market(store, now + 5, side="UP", status="SOLD", bid=.30, ask=.32)   # paper Selective sold: model flipped
    run(trader, now + 5)
    run(trader, now + 6)
    sells = [r for r in trader.client.orders.sent if r["intent"] == "ORDER_INTENT_SELL_LONG"]
    assert len(sells) == 1 and sells[0]["price"]["value"] == "0.29"
    with store.connect() as db:
        db.execute("INSERT INTO verified_markets VALUES ('m1',?,?,'DOWN',1,0,?,'f','{}')", (now - 200, now + 100, now + 110))
        trader._settle(db)
        r = db.execute("SELECT * FROM live_orders WHERE slug='m1'").fetchone()
    assert r["exit_state"] == "SOLD" and r["pnl"] == pytest.approx(r["exit_proceeds"] - r["exit_fees"] - r["cost"] - r["fees"])


def test_mode_switch_only_local_and_live_needs_confirmation(tmp_path):
    store = Store(str(tmp_path / "p.sqlite3"))
    app = create_app(store.path, collect=False, auth_required=False)
    (tmp_path / "live-keys.json").write_text(json.dumps(dict(key_id="k", secret_key="s")))
    (tmp_path / "live-convention.json").write_text(json.dumps(dict(short_price="down")))
    with TestClient(app) as c:
        assert c.post("/api/live/mode/live", headers=H).status_code == 428
        assert c.post("/api/live/mode/live", headers=dict(H, **{"X-Prophet-Confirm": "real-money"})).json() == {"mode": "LIVE"}
        assert c.get("/api/live").json()["mode"] == "LIVE"
        c.post("/api/control/emergency", headers=H)                # emergency stop turns real trading off
        assert c.get("/api/live").json()["mode"] == "OFF"
