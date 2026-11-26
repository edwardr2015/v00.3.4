"""Versioned research models. Heavy backends are optional; no automatic promotion."""

import hashlib
import importlib.util
import json
import math
from pathlib import Path
import pickle
import time

from .research import baseline_probability, fit_challenger, challenger_probability

VOTE_FAMILY = "model_vote"
VOTE_VERSION = "model-vote-v1"

CANDIDATES = {
    VOTE_FAMILY: "builtin",
    "momentum": "builtin",
    "logistic": "builtin",
    "random_forest": "sklearn",
    "gradient_boosting": "sklearn",
    "pattern_odds": "builtin",
    "xgboost": "xgboost",
    "lightgbm": "lightgbm",
    "hmm": "hmmlearn",
    "lstm": "torch",
    "ensemble": "builtin",
}
BASE_PARTICIPANTS = {
    "momentum":"Momentum", "logistic":"Logistic Regression", "random_forest":"Random Forest",
    "gradient_boosting":"Gradient Boosting", "ensemble":"Ensemble", "xgboost":"XGBoost",
    "lightgbm":"LightGBM", "pattern_odds":"Pattern & Odds Learning",
}
PARTICIPANTS = {**BASE_PARTICIPANTS, VOTE_FAMILY:"Model Vote"}
FEATURES = ["normalized_momentum_60", "normalized_trend_300", "log_volatility_15"]


def score(probabilities, ys):
    n = len(ys)
    bins = []
    for k in range(10):
        rows = [
            (p, y)
            for p, y in zip(probabilities, ys)
            if k / 10 <= p < ((k + 1) / 10 if k < 9 else 1.00001)
        ]
        bins.append(
            dict(
                lower=k / 10,
                n=len(rows),
                mean_probability=sum(p for p, y in rows) / len(rows) if rows else None,
                observed_up=sum(y for p, y in rows) / len(rows) if rows else None,
            )
        )
    return dict(
        n=n,
        brier=sum((p - y) ** 2 for p, y in zip(probabilities, ys)) / n if n else None,
        log_loss=-sum(y*math.log(max(1e-12,p))+(1-y)*math.log(max(1e-12,1-p))
                     for p,y in zip(probabilities,ys))/n if n else None,
        accuracy=sum((p >= 0.5) == bool(y) for p, y in zip(probabilities, ys)) / n if n else None,
        calibration=bins,
    )


class Models:
    def __init__(self, store):
        self.store = store
        self._artifact_cache = {}
        self.root = Path(store.path).parent / "models"
        self.root.mkdir(exist_ok=True)
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS model_registry (
              version TEXT PRIMARY KEY, kind TEXT, trained REAL, dataset TEXT,
              path TEXT, sha256 TEXT, metadata TEXT)""")
        path = self.root / "momentum-v2.json"
        raw = json.dumps(
            dict(
                kind="momentum",
                version="momentum-v2",
                features=FEATURES,
                configuration="baseline_probability v0.1",
            ),
            sort_keys=True,
        ).encode()
        if not path.exists():
            path.write_bytes(raw)
        with store.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO model_registry VALUES (?,?,?,?,?,?,?)",
                (
                    "momentum-v2",
                    "momentum",
                    None,
                    "fixed-untrained-baseline",
                    str(path.resolve()),
                    hashlib.sha256(raw).hexdigest(),
                    json.dumps(
                        dict(
                            features=FEATURES,
                            status="BASELINE",
                            profitability="UNKNOWN",
                            configuration="fixed heuristic",
                        )
                    ),
                ),
            )

        from .pattern_odds import VERSION
        pattern_path = self.root / (VERSION+".json")
        pattern_raw = json.dumps(dict(kind="pattern_odds",version=VERSION),sort_keys=True).encode()
        if not pattern_path.exists():
            pattern_path.write_bytes(pattern_raw)
        with store.connect() as db:
            db.execute("INSERT OR IGNORE INTO model_registry VALUES (?,?,?,?,?,?,?)",
                (VERSION,"pattern_odds",None,"rolling verified outcomes",str(pattern_path.resolve()),
                 hashlib.sha256(pattern_raw).hexdigest(),json.dumps(dict(status="ROLLING_RESEARCH"))))

        vote_path = self.root / (VOTE_VERSION + ".json")
        vote_record = json.dumps(dict(kind=VOTE_FAMILY,version=VOTE_VERSION),sort_keys=True).encode()
        if not vote_path.exists():
            vote_path.write_bytes(vote_record)
        with store.connect() as db:
            db.execute("INSERT OR IGNORE INTO model_registry VALUES (?,?,?,?,?,?,?)",
                (VOTE_VERSION,VOTE_FAMILY,None,"eight concurrent forecasts",str(vote_path.resolve()),
                 hashlib.sha256(vote_record).hexdigest(),json.dumps(dict(status="FORWARD_VOTE"))))

    def catalog(self):
        with self.store.connect() as db:
            rows = [
                dict(r) for r in db.execute("SELECT * FROM model_registry ORDER BY trained DESC")
            ]
        result = []
        for kind, dependency in CANDIDATES.items():
            runs = [r for r in rows if r["kind"] == kind]
            available = dependency == "builtin" or importlib.util.find_spec(dependency) is not None
            result.append(
                dict(
                    id=kind,
                    dependency=dependency,
                    status="AVAILABLE"
                    if runs and available
                    else "NOT TRAINED"
                    if available
                    else "OPTIONAL DEPENDENCY MISSING",
                    versions=runs,
                )
            )
        return result

    def sync_logistic(self):
        with self.store.connect() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='model_runs'").fetchone():
                return
            for r in db.execute("SELECT * FROM model_runs").fetchall():
                path = Path(r["path"])
                if not path.exists() or not path.resolve().is_relative_to(self.root.resolve()):
                    continue
                raw = path.read_bytes()
                if hashlib.sha256(raw).hexdigest() != r["sha256"]:
                    continue
                artifact = json.loads(raw)
                db.execute(
                    "INSERT OR IGNORE INTO model_registry VALUES (?,?,?,?,?,?,?)",
                    (
                        r["version"],
                        "logistic",
                        r["time"],
                        artifact.get("dataset_sha256", "NOT AVAILABLE"),
                        str(path.resolve()),
                        r["sha256"],
                        json.dumps(
                            dict(
                                features=FEATURES,
                                training_end=r["training_end"],
                                validation_start=r["validation_start"],
                                validation=json.loads(r["details"]),
                                profitability="UNKNOWN",
                                format="legacy-logistic-json",
                            )
                        ),
                    ),
                )

    def load(self, version):
        with self.store.connect() as db:
            r = db.execute("SELECT * FROM model_registry WHERE version=?", (version,)).fetchone()
        if not r:
            raise ValueError("Unknown model version")
        path = Path(r["path"]).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError("Model path outside private model directory")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != r["sha256"]:
            raise ValueError("Model artifact integrity failure")
        # Verify bytes on every read; deserialize each immutable version only once.
        key = (version, r["sha256"])
        if key not in self._artifact_cache:
            self._artifact_cache[key] = (
                pickle.loads(raw) if path.suffix == ".pkl" else json.loads(raw)
            )
        return dict(r), self._artifact_cache[key]

    def predict(self, version, x, sequence=None):
        r, artifact = self.load(version)
        if r["kind"] == "momentum":
            return baseline_probability(x)
        if r["kind"] == "lstm" and (sequence is None or len(sequence) < 16):
            return None  # Warm-up is expected when forward observations are sparse.
        meta = json.loads(r["metadata"])
        if meta.get("format") == "legacy-logistic-json":
            return challenger_probability(artifact, x)
        return predict_artifact(artifact, x, sequence)

    def train(self, kind, rows, now=None):
        if kind not in CANDIDATES:
            raise ValueError("Unknown model family")
        if kind == VOTE_FAMILY:
            raise ValueError("Model Vote uses same-checkpoint forecasts from all eight source models")
        if kind == "pattern_odds":
            raise ValueError("Pattern & Odds uses verified rolling settlement history")
        now = time.time() if now is None else now
        rows = sorted((r for r in rows if r["label_end"] <= now), key=lambda r: r["decision_time"])
        if len({r["slug"] for r in rows}) != len(rows):
            raise ValueError("Duplicate training markets")
        from .training_limits import describe, min_split_markets, min_training_markets

        minimum = min_training_markets()
        if len(rows) < minimum:
            raise ValueError(f"At least {minimum} chronological labelled markets required")
        a, b = (
            rows[int(0.6 * len(rows))]["decision_time"],
            rows[int(0.8 * len(rows))]["decision_time"],
        )
        train = [r for r in rows if r["label_end"] <= a]
        validation = [r for r in rows if a <= r["decision_time"] and r["label_end"] <= b]
        test = [r for r in rows if r["decision_time"] >= b]
        if min(map(len, (train, validation, test))) < min_split_markets():
            raise ValueError("Insufficient purged split coverage")
        dependency = CANDIDATES[kind]
        if dependency != "builtin" and importlib.util.find_spec(dependency) is None:
            raise ValueError("Optional dependency missing: " + dependency)
        digest = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
        version = f"{kind}-{int(now)}-{digest[:10]}"
        with self.store.connect() as db:
            existing = db.execute("SELECT 1 FROM model_registry WHERE version=?", (version,)).fetchone()
        if existing:
            saved, _ = self.load(version)
            return version, json.loads(saved["metadata"])

        artifact = fit_artifact(kind, train, validation)

        def evaluate(part):
            probs = []
            ys = []
            for i, row in enumerate(part):
                sequence = [r["x"] for r in part[max(0, i - 15) : i + 1]]
                if kind == "lstm" and len(sequence) < 16:
                    continue
                probs.append(predict_artifact(artifact, row["x"], sequence))
                ys.append(row["y"])
            return score(probs, ys)

        raw = pickle.dumps(artifact, protocol=5)
        path = self.root / (version + ".pkl")
        benchmark_test = test[15:] if kind == "lstm" else test
        from importlib import metadata as package_metadata
        import inspect

        metadata = dict(
            training_code_sha256=hashlib.sha256(
                inspect.getsource(fit_artifact).encode()
            ).hexdigest(),
            packages={
                name: package_metadata.version(name)
                for name in ("numpy", "scipy", "scikit-learn", "torch")
                if name != "torch" or kind == "lstm"
                if importlib.util.find_spec({"scikit-learn": "sklearn"}.get(name, name)) is not None
            },
            features=FEATURES,
            training_end=max(r["label_end"] for r in train),
            validation_start=a,
            test_start=b,
            trained_at=now,
            split="60/20/20 chronological; label purging",
            train_n=len(train),
            training_limits=describe(len(rows)),
            validation=evaluate(validation),
            test=evaluate(test),
            benchmarks=dict(
                coin_flip=score([0.5] * len(benchmark_test), [r["y"] for r in benchmark_test]),
                momentum=score(
                    [baseline_probability(r["x"]) for r in benchmark_test],
                    [r["y"] for r in benchmark_test]
                ),
            ),
            profitability="UNKNOWN until causal execution replay or fresh forward simulation",
            configuration=artifact.get("configuration", {}),
            format="locally-generated-pickle",
        )
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM model_registry WHERE version=?", (version,)).fetchone():
                saved, _ = self.load(version)
                return version, json.loads(saved["metadata"])
            temp = path.with_suffix(".tmp")
            temp.write_bytes(raw)
            temp.replace(path)
            db.execute(
                "INSERT INTO model_registry VALUES (?,?,?,?,?,?,?)",
                (
                    version,
                    kind,
                    now,
                    digest,
                    str(path.resolve()),
                    hashlib.sha256(raw).hexdigest(),
                    json.dumps(metadata),
                ),
            )
        return version, metadata


def feature_sequence(store, x, checkpoint, slug, now):
    """Past market observations only, independent of whether a model could predict."""
    with store.connect() as db:
        rows = db.execute(
            """SELECT details FROM v2_decisions WHERE checkpoint=? AND slug!=? AND time<=?
                AND json_type(details,'$.momentum_60') IN ('integer','real')
                AND json_type(details,'$.trend_300') IN ('integer','real')
                AND json_type(details,'$.volatility') IN ('integer','real')
                ORDER BY time DESC LIMIT 15""",
            (checkpoint, slug, now),
        ).fetchall()
    result = []
    for row in reversed(rows):
        d = json.loads(row["details"])
        if all(k in d for k in ("momentum_60", "trend_300", "volatility")):
            result.append([d["momentum_60"], d["trend_300"], math.log(max(d["volatility"], 1e-7))])
    return result + [x]


def fit_artifact(kind, train, validation):
    x = [r["x"] for r in train]
    y = [r["y"] for r in train]
    if len(set(y)) < 2:
        raise ValueError("Both outcomes required in training set")
    if kind == "momentum":
        return dict(kind=kind)
    if kind in ("logistic", "ensemble"):
        logistic = fit_challenger(train)
        artifact = dict(kind=kind, model=logistic, configuration=dict(seed=42, ensemble_weight=0.5))
        if kind == "ensemble":
            calibration = []
            for row in validation:
                p = 0.5 * baseline_probability(row["x"]) + 0.5 * challenger_probability(
                    logistic, row["x"]
                )
                calibration.append(dict(x=[math.log(p / (1 - p)), 0, 0], y=row["y"]))
            if len(calibration)>=100 and len({r["y"] for r in calibration})==2:
                artifact["calibrator"] = fit_challenger(calibration)
                artifact["configuration"]["calibration"] = "logistic on validation only; test untouched"
            else:
                artifact["configuration"]["calibration"] = "waiting for 100 validation rows with both outcomes"
        return artifact
    if kind == "random_forest":
        from sklearn.ensemble import RandomForestClassifier

        model = RandomForestClassifier(
            n_estimators=150, max_depth=4, min_samples_leaf=20, random_state=42, n_jobs=1
        )
    elif kind == "gradient_boosting":
        from sklearn.ensemble import GradientBoostingClassifier

        model = GradientBoostingClassifier(n_estimators=100,max_depth=2,min_samples_leaf=15,random_state=42)
    elif kind == "xgboost":
        from xgboost import XGBClassifier

        model = XGBClassifier(
            n_estimators=100,
            max_depth=2,
            learning_rate=0.03,
            random_state=42,
            n_jobs=1,
            eval_metric="logloss",
        )
    elif kind == "lightgbm":
        from lightgbm import LGBMClassifier

        model = LGBMClassifier(
            n_estimators=100,
            max_depth=3,
            num_leaves=7,
            learning_rate=0.03,
            random_state=42,
            n_jobs=1,
            verbosity=-1,
        )
    elif kind == "hmm":
        import numpy as np
        from hmmlearn.hmm import GaussianHMM

        means = np.mean(x, axis=0)
        scales = np.std(x, axis=0)
        scales[scales == 0] = 1
        values = (np.array(x) - means) / scales
        model = GaussianHMM(
            n_components=2, covariance_type="diag", n_iter=100, random_state=42
        ).fit(values)
        artifact = dict(
            kind=kind,
            model=model,
            means=means,
            scales=scales,
            configuration=dict(seed=42, states=2, filter="causal forward"),
        )
        # State/outcome mapping uses training data only; inference never smooths future rows.
        state = filtered_states(model, values)
        artifact["state_up"] = (state.T @ np.array(y) + 1) / (state.sum(axis=0) + 2)
        return artifact
    elif kind == "lstm":
        import numpy as np
        import torch

        torch.manual_seed(42)
        torch.set_num_threads(1)
        if len(train) < 2000:
            raise ValueError("LSTM requires 2000 training markets plus validation/test sequences")
        means = np.mean(x, axis=0)
        scales = np.std(x, axis=0)
        scales[scales == 0] = 1
        values = (np.array(x) - means) / scales
        seq = torch.tensor(
            np.array([values[i - 15 : i + 1] for i in range(15, len(values))]), dtype=torch.float32
        )
        labels = torch.tensor(y[15:], dtype=torch.float32)
        model = torch.nn.LSTM(3, 8, batch_first=True)
        head = torch.nn.Linear(8, 1)
        optimizer = torch.optim.Adam(list(model.parameters()) + list(head.parameters()), lr=0.003)
        for _ in range(30):
            optimizer.zero_grad()
            out, _ = model(seq)
            loss = torch.nn.functional.binary_cross_entropy_with_logits(
                head(out[:, -1, :]).squeeze(-1), labels
            )
            loss.backward()
            optimizer.step()
        return dict(
            kind=kind,
            lstm=model.state_dict(),
            head=head.state_dict(),
            means=means,
            scales=scales,
            configuration=dict(seed=42, sequence=16, hidden=8, epochs=30),
        )
    else:
        raise ValueError("Unsupported family")
    model.fit(x, y)
    return dict(kind=kind, model=model, configuration=model.get_params())


def filtered_states(model, values):
    import numpy as np

    emissions = np.exp(model._compute_log_likelihood(values))
    state = model.startprob_.copy()
    result = []
    for i, emission in enumerate(emissions):
        state = (state if i == 0 else state @ model.transmat_) * emission
        state = state / max(state.sum(), 1e-300)
        result.append(state.copy())
    return np.array(result)


def predict_artifact(artifact, x, sequence=None):
    kind = artifact["kind"]
    if kind == "momentum":
        p = baseline_probability(x)
    elif kind == "logistic":
        p = challenger_probability(artifact["model"], x)
    elif kind == "ensemble":
        # Fixed equal-weight ensemble; calibration is measured, never presumed.
        p = 0.5 * baseline_probability(x) + 0.5 * challenger_probability(artifact["model"], x)
        if artifact.get("calibrator"):
            p = challenger_probability(artifact["calibrator"], [math.log(p / (1 - p)), 0, 0])
    elif kind == "hmm":
        import numpy as np

        values = (np.array(sequence or [x]) - artifact["means"]) / artifact["scales"]
        p = float(filtered_states(artifact["model"], values)[-1] @ artifact["state_up"])
    elif kind == "lstm":
        import numpy as np
        import torch

        if not sequence or len(sequence) < 16:
            raise ValueError("LSTM needs 16 past feature observations")
        # Small CPU recurrent batches are slower with large BLAS worker pools.
        if torch.get_num_threads() != 1:
            torch.set_num_threads(1)
        if "_inference" not in artifact:
            model = torch.nn.LSTM(3, 8, batch_first=True)
            model.load_state_dict(artifact["lstm"])
            head = torch.nn.Linear(8, 1)
            head.load_state_dict(artifact["head"])
            artifact["_inference"] = (model.eval(), head.eval())
        model, head = artifact["_inference"]
        values = (np.array(sequence[-16:]) - artifact["means"]) / artifact["scales"]
        with torch.no_grad():
            out, _ = model(torch.tensor(values[None, :, :], dtype=torch.float32))
            p = float(torch.sigmoid(head(out[:, -1, :])).item())
    elif kind == "lightgbm":
        import numpy as np

        p = float(artifact["model"].booster_.predict(np.array([x]))[0])
    else:
        p = float(artifact["model"].predict_proba([x])[0][1])
    if not math.isfinite(p):
        raise ValueError("Non-finite model probability")
    return max(0.001, min(0.999, p))
