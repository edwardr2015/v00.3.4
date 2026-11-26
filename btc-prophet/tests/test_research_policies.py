"""Settlement-average model and frozen research policies (fixtures only)."""
import json

from prophet import corrective, settlement_model as sm
from prophet.app import create_app
from test_timing_update import market_inputs
from test_v2 import setup

TERMS = dict(theta=.095, tick=.01, quantum=.01)


def test_settlement_average_probability_shape():
    assert abs(sm.probability(100, 100, .001, 200) - .5) < 1e-9
    early, late = sm.probability(100.05, 100, .001, 200), sm.probability(100.05, 100, .001, 30)
    assert .5 < early < late
    # Inside the window the fixed part dominates: a fixed average above the open is near-certain UP
    assert sm.probability(99.99, 100, .001, 2, [100.2] * 58) > .95
    assert sm.probability(100.01, 100, .001, 2, [99.8] * 58) < .05
    # Averaging narrows the distribution versus the single-print gap formula at equal volatility
    assert sm.probability(100.05, 100, .001, 200, vol_scale=1) > .5 + 1e-6
    assert sm.probability(None, 100, .001, 200) is None


def _info(now, left, ref=101, opening=100):
    return dict(decision=now, seconds_left=left, reference_price=ref, opening=opening,
                volatility=.001, rejections={})


def test_research_policies_wait_for_start_then_enter_once_without_ledger_writes(tmp_path):
    s, _, now = setup(tmp_path)
    app = create_app(s.path, collect=False, auth_required=False)
    research = corrective.Research(s, app.state.paper)
    book = dict(bids=[dict(price=.55, quantity=500)], asks=[dict(price=.57, quantity=500)])
    with s.connect() as db:
        early = _info(now, 250)
        research.research_policy_trials(db, "fixture-round", .97, .56, dict(volatility=.001), book, TERMS, early)
        assert early["rejections"]["late_gap_180s_abs10"] == "BEFORE_START"
        late = _info(now + 100, 100)
        research.research_policy_trials(db, "fixture-round", .97, .56, dict(volatility=.001), book, TERMS, late)
        assert late["rejections"]["late_gap_180s_abs10"] == dict(standard="ELIGIBLE", higher="ELIGIBLE")
        again = _info(now + 101, 99)
        research.research_policy_trials(db, "fixture-round", .97, .56, dict(volatility=.001), book, TERMS, again)
        rows = db.execute("SELECT policy,cost_case,side,evidence FROM corrective_trials").fetchall()
        assert sum(r["policy"] == "late_gap_180s_abs10" for r in rows) == 2   # still one per cost case
        assert {(r["policy"], r["cost_case"]) for r in rows} >= {("late_gap_180s_abs10", "standard"),
                                                                ("late_gap_180s_abs10", "higher")}
        assert all(r["side"] == "UP" for r in rows)
        assert json.loads(rows[0]["evidence"])["probability"] > .9
        assert db.execute("SELECT count(*) FROM unified_trades").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM v2_orders").fetchone()[0] == 0


def test_absolute_edge_rejects_when_price_is_too_close():
    book = dict(bids=[dict(price=.88, quantity=500)], asks=[dict(price=.90, quantity=500)])
    assert corrective.absolute_choice(book, TERMS, "UP", .95, .10)[1] == "EDGE_TOO_SMALL"
    assert corrective.absolute_choice(book, TERMS, "UP", .99, .05)[1] == "ELIGIBLE"


def test_real_tick_lists_new_policies_and_records_settlement_challenger(tmp_path, monkeypatch):
    s, _, now = setup(tmp_path)
    app = create_app(s.path, collect=False, auth_required=False)
    p = app.state.paper
    p.mode("SIMULATE", now)
    research = corrective.Research(s, p)
    monkeypatch.setattr(p, "probability", lambda *_a, **_k: (.85, dict(momentum_60=.005, trend_300=.004, volatility=.001)))
    market_inputs(s, now + 2, bid=.51, ask=.53)
    s.put("reference", dict(s.get("reference"), price=101))
    research.tick(now + 2)
    with s.connect() as db:
        evidence = json.loads(db.execute("SELECT evidence FROM corrective_observations ORDER BY id DESC")
                              .fetchone()[0])
    for name in (*corrective.RESEARCH_POLICIES, "primary_rule_edge0_40_70"):
        assert name in evidence["rejections"]
    assert evidence["settlement_average_up"] is not None
