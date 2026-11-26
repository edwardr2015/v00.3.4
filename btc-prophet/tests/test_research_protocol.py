import math

import pytest

from prophet.research import (
    baseline_probability,
    challenger_probability,
    chronological_split,
    decision,
    features,
    fit_challenger,
    metrics,
)


def test_features_never_read_future_or_partial_candles():
    t = 1800000000
    bars = {t - 315 + i * 15: dict(close=100 + i, ticks=15) for i in range(21)}
    initial = features(bars, t)
    assert initial is not None
    bars[t] = dict(close=1000000, ticks=15)
    bars[t + 15] = dict(close=0.0001, ticks=15)
    assert features(bars, t) == initial
    bars[t - 15]["ticks"] = 14
    assert features(bars, t) is None


def test_split_purges_labels_crossing_training_boundary():
    start = 1800000000
    rows = [
        dict(decision_time=start + 4 * 86400 - 90, label_end=start + 4 * 86400 + 1),
        dict(decision_time=start + 4 * 86400 - 210, label_end=start + 4 * 86400),
        dict(decision_time=start + 4 * 86400 + 90, label_end=start + 4 * 86400 + 300),
        dict(decision_time=start + 5 * 86400 + 90, label_end=start + 5 * 86400 + 300),
    ]
    parts = chronological_split(rows, start, start + 7 * 86400)
    assert parts == {"train": [rows[1]], "validation": [rows[2]], "test": [rows[3]]}
    with pytest.raises(ValueError):
        chronological_split(rows, start, start + 6 * 86400)


def test_baseline_skips_and_probability_is_bounded():
    assert decision(baseline_probability([0, 0, -5])) == "SKIP"
    assert decision(baseline_probability([5, 5, -5])) == "UP"
    assert decision(baseline_probability([-5, -5, -5])) == "DOWN"
    assert 0.29 < baseline_probability([-100, -100, 100]) < 0.71


def test_challenger_preprocessing_is_training_only_and_artifact_is_not_promoted():
    train = [dict(x=[float(i % 4), float(i % 3), -5.0], y=i % 2) for i in range(100)]
    model = fit_challenger(train)
    assert model["means"][2] == -5
    assert model["means"][0] == 1.5
    assert model["promoted"] is False
    assert math.isfinite(challenger_probability(model, [1000, -1000, 10]))


def test_research_metrics_never_invent_trading_pnl():
    rows = [dict(x=[0, 0, -5], y=1, quote_age=30), dict(x=[1, 1, -5], y=0, quote_age=60)]
    result = metrics(rows, baseline_probability)
    assert result["examples"] == 2
    assert result["pnl"] is None and result["hypothetical_trades"] == 0
    assert result["quotes_no_older_than_15_seconds"] == 0
