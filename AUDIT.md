# BTC Prophet — Continuity and Readiness Audit (2026-11-26)

Read-only audit. No application code, database, balance or model artifact was modified.
Real-money execution was not touched (it is disabled in every audited release).

## 1. What was actually available

| Location | Contents |
|---|---|
| `edwardr2015/v00.3.4` | **Empty** before this commit (no commits on any branch) |
| `edwardr2015/001.200.0` `main` / `development-v0.3.4` @ `2dd44dd` | Release ZIPs `001.200.1`, `000.200.002`, `000.200.003` + README |
| Newest source found | `000.200.003.zip` → `pyproject.toml` version **0.2.3** (2026-11-22) |
| **Not found anywhere reachable** | v0.3.23 source; the ~177 MB original DB; the research DB; any `history.sqlite3`; trained model artifacts; the five Excel reports |

Files named in the directive that do **not exist** in the newest available source:
`prophet/autonomous.py`, `prophet/consensus.py`, `prophet/exit_research.py`,
`prophet/hold_research.py`, `prophet/research_audit.py`. These belong to a later version
than anything on GitHub.

## 2. Confirmed defects (verified in v0.2.3 source; re-check in v0.3.23)

1. **Database path changed between versions → learning disconnected.**
   `prophet/app.py` defaults to `data/prophet.sqlite3` and `data/history.sqlite3`, but every
   `Start Prophet.command` since `001.200.1` exports
   `PROPHET_DB=$PWD/data/timing/prophet.sqlite3` and
   `PROPHET_HISTORY_DB=$PWD/data/timing/history.sqlite3`. An install that first ran under
   the old default keeps its history in `data/`; the launcher silently creates a fresh,
   empty DB in `data/timing/`. `Learning.train` only reads the configured store and history
   DB, so the 47 labels / 286 lab predictions in the original DB are invisible. This
   matches the reported 15,972→2,038 candles and 47→1 labels exactly in shape.
2. **`PROPHET_FRESH_RESEARCH=1` is the launcher default** and makes
   `collector.py:171` skip the 60-market history bootstrap, so a new DB never backfills.
3. **Displayed gate is not the real gate.** `learning.py` reports "Need 200 labelled
   examples", but the five optional challengers are trained by `Models.train`, which
   requires **300** unique markets and ≥30 in each purged 60/20/20 partition
   (`models.py:186-196`). Reaching 200 trains only the logistic challenger; the "93 of 200"
   progress bar overstates challenger readiness.
4. **LSTM needs 2,000 markets** (`models.py:360`) and `torch` is not in the `research`
   extras, so it cannot train on a standard install.
5. **Model count is 8, not 13.** `models.CANDIDATES` = momentum, logistic, random_forest,
   xgboost, lightgbm, hmm, lstm, ensemble. The "five challengers"
   (`ensemble,random_forest,hmm,lightgbm,xgboost`) are a *subset* of those eight, not five
   additional models. There is **no Gradient Boosting** model; the fifth challenger is HMM.
6. **Training mixes decision offsets.** History examples are built at +60 s
   (`learning.py:86`) while forward labels use whatever time the prediction was made.
   Not leakage, but one model is fit across inconsistent entry times.

## 3. Requirements checklist (against v0.2.3, the only code available)

| Requirement | Status |
|---|---|
| Real-money execution disabled | Complete |
| Paper accounts separate (Observer, 24 agreement scenarios, 144 lab, 3 adaptive) | Complete |
| Official-settlement labels, chronological purged splits | Complete |
| 200-example learning gate | Complete but misleading (see defect 3) |
| Learning continuity across upgrades | **Broken** (defects 1–2) |
| 13 forecasting candidates | **Missing** — 8 defined; Gradient Boosting absent |
| Model Vote shown separately | Not present in v0.2.3 |
| Automatic primary selection with calibration/correlation | **Missing** — primary is fixed momentum; switching is a manual `promotion.promote` with a written reason, hold-to-settlement, stake ≤ $3 |
| Model pinning during open positions | Not verifiable here (`autonomous.py` absent) |
| Selective-trading research accounts A–J | Partial — agreement scenarios at 65% / 3% / 5% edge exist; others missing |
| Early-exit research | Archived; all positions hold to settlement |
| Feed freshness (CONNECTED vs FRESH) | Partial — `paper.py` rejects reference/book older than 15 s, provider health older than 20 s |
| Unified dashboard | Partial — Observer dashboard + separate `/research-dashboard` |

## 4. Tests run

| Suite | Result |
|---|---|
| v0.2.3 full suite (`000.200.003`, Python 3.13, `.[dev,research]`) | **100 passed, 1 skipped** in 17 s |
| `tests/test_continuity_audit.py` (new) | **4 passed** |
| Audit tool against DBs created by the real v0.2.3 app | Passed (schema compatible) |

Not run (need your data or the 0.3.23 source): simulation reproduction, training on real
data, Excel reconciliation, browser tests of 0.3.23, Mac-specific checks.

## 5. Simulation results

**Not reproduced.** The 62-trade figures (+$13.01, +$14.72 hold, the confidence and
timing cohorts) and the Gap Agreement table can only be recomputed from the database that
holds those orders. None is available here. No figure from the directive has been
independently verified in this session.

## 6. Run the continuity audit on your Mac

Read-only; safe while the app runs. From the project folder:

```bash
python3 tools/continuity_audit.py \
  --store original=data/prophet.sqlite3 \
  --store timing=data/timing/prophet.sqlite3 \
  --history original=data/history.sqlite3 \
  --history timing=data/timing/history.sqlite3 \
  --json continuity-audit.json
```

Drop any `--store`/`--history` line whose file does not exist, and add any other
`*.sqlite3` you find (`find ~ -name '*.sqlite3' -size +1M 2>/dev/null`). The report gives
per-database table counts, rejection reasons, cross-database outcome conflicts, the number
of unique eligible markets, and the exact shortage against the 200 / 300 / 2,000 gates.
`--export data/isolated-dataset.sqlite3` writes a deduplicated isolated dataset to a new
file and refuses to overwrite. Duplicated or conflicting markets are never counted.

Rollback: the tool only adds `tools/` and `tests/`; delete them to roll back.
