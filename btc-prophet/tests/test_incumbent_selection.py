"""Primary-account selection against the incumbent (fixtures only)."""
import json

from prophet.app import create_app
from prophet.model_paper import FORMULA_VERSIONS, ModelPaper
from test_v2 import setup

INCUMBENT = FORMULA_VERSIONS["volatility_reference_gap"]


def market(db, k, now, outcome="UP", verified_delay=20):
    slug = f"fixture-{k:03d}"
    start = now - 400 - k * 300
    db.execute("INSERT INTO model_paper_windows VALUES (?,?,?,?,?)", (slug, start, start + 300, "{}", start + 300))
    db.execute("INSERT INTO verified_markets VALUES (?,?,?,?,?,?,?,?,?)",
               (slug, start, start + 300, outcome, 100, 101, start + 300 + verified_delay, "fixture", "{}"))
    return slug, start


def position(db, family, version, slug, start, side, up, outcome, quantity=10, cost=5):
    t = start + 90
    db.execute("""INSERT INTO model_paper_positions VALUES (?,?,?,?,?,?,?,?,?,'SETTLED',?,?,?,0,?)""",
               (family, slug, version, side, t, quantity, cost, "{}", .1, t + 210, outcome,
                quantity * (side == outcome), quantity * (side == outcome) - cost))
    db.execute("""INSERT INTO model_paper_decisions(family,slug,time,version,up,action,reason,observation,evidence)
                  VALUES (?,?,?,?,?,?,?,?,?)""", (family, slug, t, version, up, side, "fixture", f"{family}{slug}", "{}"))


def build(tmp_path, n=24):
    s, _, now = setup(tmp_path)
    app = create_app(s.path, collect=False, auth_required=False)
    selection = app.state.paper.continuous.selection
    with s.connect() as db:
        current = ModelPaper.versions(db, now)
        for k in range(n):
            slug, start = market(db, k, now)
            # Incumbent wrong every time; challenger right every time but disagrees with it.
            position(db, "volatility_reference_gap", INCUMBENT, slug, start, "DOWN", .40, "UP")
            position(db, "market_odds_adjustment", current["market_odds_adjustment"], slug, start, "UP", .70, "UP")
            # Copycat: always the incumbent's side (redundant), and Model Vote (derived, never selectable)
            position(db, "pattern_odds", current.get("pattern_odds", "pattern-odds-v1"), slug, start, "DOWN", .45, "UP")
            position(db, "model_vote", "model-vote-v1", slug, start, "UP", .90, "UP")
    return s, selection, now, current


def test_selects_challenger_that_beats_incumbent_and_rejects_copies_and_vote(tmp_path):
    s, selection, now, current = build(tmp_path)
    _, ranked = selection.rank_incumbent("live", .0005, now, INCUMBENT)
    assert [r["family"] for r in ranked] == ["market_odds_adjustment"]
    best = ranked[0]
    assert best["matched"] == 24 and best["compared_with"] == INCUMBENT
    assert best["improvement"] > 0 and best["agreement"] == 0 and best["brier"] < best["incumbent_brier"]


def test_needs_enough_settled_markets_and_ignores_unverified(tmp_path):
    s, selection, now, _ = build(tmp_path, n=19)
    assert selection.rank_incumbent("live", .0005, now, INCUMBENT)[1] == []
    s, selection, now, _ = build(tmp_path / "late", n=24)
    # Evaluated before any market was verified: nothing counts yet.
    assert selection.rank_incumbent("live", .0005, now - 24 * 300 - 400, INCUMBENT)[1] == []


def test_losing_challenger_is_not_selected(tmp_path):
    s, _, now = setup(tmp_path)
    app = create_app(s.path, collect=False, auth_required=False)
    selection = app.state.paper.continuous.selection
    with s.connect() as db:
        current = ModelPaper.versions(db, now)
        for k in range(24):
            slug, start = market(db, k, now)
            position(db, "volatility_reference_gap", INCUMBENT, slug, start, "UP", .70, "UP")
            position(db, "market_odds_adjustment", current["market_odds_adjustment"], slug, start, "DOWN", .40, "UP")
    assert selection.rank_incumbent("live", .0005, now, INCUMBENT)[1] == []


def test_predict_uses_winner_formula_and_logs_evidence(tmp_path):
    s, selection, now, current = build(tmp_path)
    features = dict(momentum_60=.1, trend_300=.1, volatility=.001)
    book = dict(bids=[dict(price=.55, quantity=100)], asks=[dict(price=.57, quantity=100)])
    context = dict(features, reference_price=100.2, window_end=now + 120, momentum_up=.6)
    model, up, evidence = selection.predict("live", now, .62, features, INCUMBENT,
                                            pattern_context=(context, book, 100.0))
    assert model == current["market_odds_adjustment"]
    assert 0 < up < 1 and evidence["family"] == "market_odds_adjustment"
    assert json.dumps(evidence)   # serialisable for paper_model_choices
