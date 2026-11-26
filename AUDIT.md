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
| `tests/test_continuity_audit.py` + `tests/test_reproduce_primary.py` (new) | **5 passed** |
| Audit tool against DBs created by the real v0.2.3 app | Passed (schema compatible) |

Not run (need your data or the 0.3.23 source): simulation reproduction, training on real
data, Excel reconciliation, browser tests of 0.3.23, Mac-specific checks.

## 5. Simulation results — reproduced from Quick Report 4 (generated 2026-11-26 14:38 UTC)

Source: `BTC-Prophet-Quick-Report-4.xlsx` (SHA-256 `a554f511…51fa14`), sheet
*Unified Trades*, recomputed by `tools/reproduce_primary.py`. Report 3 contains the same 62
trades and gives identical results. Reports 1 and 2 are earlier snapshots (4 trades).

| Cohort | Trades | Wins | Net P&L | $/staked | Max DD | Win 95% CI | Matches directive |
|---|---|---|---|---|---|---|---|
| Existing strategy | 62 | 34 | +$13.01 | 0.042 | $29.01 | 0.43–0.67 | yes |
| Hold to settlement | 62 | 39 | +$14.72 | 0.048 | $49.01 | 0.50–0.74 | yes |
| Confidence ≥ 60% | 50 | 28 | +$4.00 | 0.016 | $22.90 | 0.42–0.69 | yes |
| Confidence ≥ 70% | 27 | 18 | +$19.10 | 0.142 | $9.86 | 0.48–0.81 | yes |
| Confidence ≥ 80% | 5 | 4 | +$5.83 | 0.234 | $2.69 | 0.38–0.96 | yes |
| Confidence ≥ 90% | 0 | – | – | – | – | – | yes |
| Entry < 90 s | 43 | 22 | +$12.22 | 0.057 | $18.93 | 0.37–0.65 | yes |
| Entry ≥ 90 s | 19 | 12 | +$0.79 | 0.008 | $16.64 | 0.41–0.81 | yes |

Definitions: confidence = UP estimate for UP entries, 1 − estimate for DOWN entries; entry
second = entry time − open parsed from the slug; hold = quantity × (side = outcome) − cost.

What the reproduction adds:
* **Early exits:** 35 of 62 trades were sold early. They lost $33.39; held, they would
  have lost $31.68 (exit cost $1.71). Holding had a **much larger drawdown ($49 vs $29)**,
  so early exits did reduce risk while costing a little P&L.
* **Confidence is not monotonic.** ≥60% earns less than all trades ($4.00 vs $13.01):
  the 12 trades at 50–60% made +$9.01. Every confidence interval overlaps every other one.
  None of these cohorts is statistically distinguishable on 62 trades.
* **+240 s exit** (*Late Exit Comparison*): hold −$7.37 vs exit −$20.84 (standard costs,
  76 matched fills, 48 executed exits), i.e. exit worse by $13.47; higher costs: worse by
  $14.46. Confirms that it underperformed holding.

**Gap Agreement table (directive §4):** every row matches the app's *Forward Research*
sheet exactly (40–70¢ −$3.72, conf65 +$8.11, conf75 +$21.63, edge10 +$22.33, calibrated
+$17.56). These were checked against the sheet's totals; per-trade detail for these
policies is not in the Quick Report, so they were not independently recomputed.

Other rows from the same sheet (same 94 markets, standard costs) relevant to choosing
forward tests: `ewma_gap_90s` 28/36 +$25.36; `student_t_gap_90s` 30/39 +$22.48;
`90s_gap_agreement_40_70` 11/12 +$22.82; `kalman_trend_90s` 30/41 +$13.53;
`gap_0.40_0.49` 21/32 +$54.93; `gap_eligible_control` 53/94 **−$64.97**.
High-win-rate bands make almost nothing: `gap_0.90_0.94` 35/36 wins +$8.85 and
`gap_0.95_0.99` 36/36 wins +$6.95. **A 90%+ win rate is achievable only by buying
contracts priced near $1, which leaves almost no profit.** About 58 policy/cost rows were
evaluated on the same 94 markets, so the best rows are expected to look good by chance;
none should be promoted without untouched forward markets.

## 5a. Defects found in the reports (v0.3.23 behaviour)

7. **Automatic selection has never selected anything.** All 64 primary-model observations
   carry `model_evidence.reason = NO_COMPARABLE_WINNER` (`minimum_matched: 20`). Every one
   of the 62 trades ran on the fallback `volatility-reference-gap-v1`. The scoreboard shows
   **0 matched challenger forecasts**, so challengers are not producing forward forecasts.
8. **"Calibrated" probabilities used 0–56 labels**, mostly under 10
   (`calibration_labels` values 0,1,2,…,10,17,18,19,49,56). Those are effectively uncalibrated.
9. **SKIP reasons are lost.** All 31 non-entry rounds end with a `momentum-book-v0.2`
   record at 298–300 s elapsed reading `SKIP: NO_LIQUIDITY`, which overwrites the real reason
   earlier in the round. The *Recent Skips* sheet is empty despite 32 skipped rounds.
10. **Possible stale-book fills (highest priority to verify).** 23 of 62 entries were
    filled when `book_checked − book_timestamp` was 15–30 s. Those 23 made **+$26.09**
    (16 wins); the 39 entries on fresher books **lost $13.08** (18 wins). If
    `book_timestamp` is the book's capture time, these fills may not have been executable and
    the account's whole profit depends on them. If it is the last-change time with a fresh
    venue confirmation, they are valid. Needs the 0.3.23 source to decide.
11. **Higher-cost variants sometimes earn more than standard costs** (e.g. 40–70¢:
    −$3.72 → +$15.62). Higher costs alone cannot improve identical trades, so the cost-stress
    variant also changes which trades are taken. It is not a pure sensitivity test.
12. **Selective-90 has made 0 calls in 94 markets** (blockers: opening move too small 54,
    low momentum score 39, missed checkpoint 1).

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
