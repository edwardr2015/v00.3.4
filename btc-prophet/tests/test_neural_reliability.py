"""Neural artifact and input-history regression tests using synthetic rounds only."""
import json
import math
from pathlib import Path

import pytest

from prophet.models import Models, feature_sequence
from test_v2 import setup


def test_sequence_recovers_older_complete_rows_after_missing_ticks(tmp_path):
    store, _, now = setup(tmp_path)
    with store.connect() as db:
        for i in range(28):
            detail = (dict(momentum_60=i, trend_300=i, volatility=.01)
                      if i < 15 else dict(reason="MISSING_DATA"))
            db.execute("INSERT INTO v2_decisions VALUES (?,?,?,?,?,?,?,?)",
                       (f"fixture-{i}",60,now-60+i,"fixture",None,"SKIP","TEST",json.dumps(detail)))
        db.execute("INSERT INTO v2_decisions VALUES (?,?,?,?,?,?,?,?)",
                   ("future",60,now+1,"fixture",.5,"SKIP","TEST",
                    json.dumps(dict(momentum_60=999,trend_300=999,volatility=.01))))
    sequence = feature_sequence(store, [-1,0,-5], 60, "current", now)
    assert [r[0] for r in sequence] == list(range(15))+[-1]


def test_neural_train_retry_reload_warmup_and_corruption(tmp_path):
    torch = pytest.importorskip("torch", reason="Optional neural dependency")
    store, _, _ = setup(tmp_path)
    models = Models(store)
    rows = []
    for i in range(3440):
        x = [math.sin(i/7),math.cos(i/11),-5+math.sin(i/13)/10]
        rows.append(dict(slug=f"lstm-fixture-{i}",decision_time=300*i,
                         label_end=300*i+240,x=x,y=int(x[0]+x[1]/2>0)))
    train_at = rows[-1]["label_end"] + 10
    version, metrics = models.train("lstm",rows,train_at)
    assert metrics["training_end"]<=metrics["validation_start"]<metrics["test_start"]
    assert metrics["benchmarks"]["momentum"]["n"]==metrics["test"]["n"]
    assert metrics["packages"]["torch"]==torch.__version__
    model, _ = models.load(version)
    path = Path(model["path"])
    content = path.read_bytes()
    retry, retry_metrics = Models(store).train("lstm",rows,train_at)
    assert retry==version and retry_metrics==metrics and path.read_bytes()==content
    recent = [r["x"] for r in rows[-16:]]
    assert Models(store).predict(version,recent[-1],recent[:-1]) is None
    p = models.predict(version,recent[-1],recent)
    assert p==Models(store).predict(version,recent[-1],recent)
    assert 0<p<1
    path.write_bytes(content+b"damaged")
    with pytest.raises(ValueError,match="integrity"):
        Models(store).predict(version,recent[-1],recent)
