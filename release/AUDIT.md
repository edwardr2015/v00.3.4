# BTC Prophet 0.3.23 — Continuation Audit (2026-11-26)

Real-money execution remains disabled. No database, balance, settlement history or model
artifact was touched: none of them were supplied. All code changes are paper/reporting only.

## 1. Inputs

| Item | Source | SHA-256 |
|---|---|---|
| `000.300.023.zip` → `pyproject` version **0.3.23** | `edwardr2015/001.200.0@17b8f91` | `a8531e63ad60…7a23ba3ffc7243fd0a` |
| `000.300.023Rzip` | same commit | identical to the above (duplicate) |
| `000.300.021.zip` | same commit | `4948000943fa…1b7ef7b90b1247a7c2` |
| Quick Reports 1–4 (`.xlsx`) | chat upload | Report 4: `a554f511…51fa14` |
| `BTC-Prophet-Paper-Report.xlsx` | **not received** | – |
| Original ~177 MB DB, research DB, `history.sqlite3`, `models/` | **not received** | – |

Repository layout on branch `claude/btc-prophet-completion-9ym8j7`:
`btc-prophet/` = the 0.3.23 app (commit `a764353` is the **unmodified** ZIP contents, so
every later change is a separate, revertible commit); `tools/` = read-only audit tools;
`tests/` = tests for those tools.

## 2. Forecasting-candidate readiness matrix

Evidence: 0.3.23 source and Quick Report 4 (94 verified markets, 2026-11-26 06:48–14:38 UTC).
"Trading" means the family recorded settled $3 model-account fills in Report 4.

| # | Candidate | Kind | Installed | Trained | Producing forward forecasts | Primary-selectable | Report 4 result |
|---|---|---|---|---|---|---|---|
| 1 | Momentum | original, fixed heuristic | yes | n/a (fixed) | **yes** | baseline only | 36/94, −$49.64 |
| 2 | Logistic Regression | original | yes | **no** (needs 200 labels) | no | when trained | – |
| 3 | Random Forest | original ML | yes (sklearn) | **no** (needs 300) | no | when trained | – |
| 4 | Gradient Boosting | original ML | yes (sklearn) | **no** (needs 300) | no | when trained | – |
| 5 | Ensemble | original ML | yes | **no** (needs 300) | no | when trained | – |
| 6 | XGBoost | original ML | yes if installed | **no** (needs 300) | no | when trained | – |
| 7 | LightGBM | original ML | yes if installed | **no** (needs 300) | no | when trained | – |
| 8 | Pattern & Odds Learning | original, rolling | yes | rolling, no artifact | **yes** | yes | 29/74, −$24.09 |
| 9 | Volatility-Adjusted Reference Gap | formula challenger | yes | n/a (frozen formula) | **yes** — current primary fallback | no (not in selector pool) | 56/94, **+$24.26** |
| 10 | Market Odds Adjustment | formula challenger | yes | n/a | **yes** | no | 27/88, −$48.07 |
| 11 | EWMA gap @90 s | corrective shadow | yes | n/a | yes (shadow only) | no | 28/36, +$25.36 |
| 12 | Student-t gap @90 s | corrective shadow | yes | n/a | yes (shadow only) | no | 30/39, +$22.48 |
| 13 | Kalman trend @90 s | corrective shadow | yes | n/a | yes (shadow only) | no | 30/41, +$13.53 |
| 14 | Nearest neighbour @90 s | corrective shadow | yes | n/a | yes (shadow only) | no | 0/7, −$34.99 |
| – | HMM | legacy | optional extra | – | **archived** by `lab.refresh` | no | – |
| – | LSTM | legacy | needs `torch` | needs 2,000 markets | **archived** | no | – |
| – | Model Vote | **derived consensus** | yes | n/a | records votes; "All 10 agree" never fills | not independent | 0 fills / 95 |
| – | Reference Gap + Market Agreement | rule on #9 + market | yes | n/a | yes | no | 65/94, −$5.84 ($3); 66/94, **−$153.18** ($100 pooled) |

**Counts:** 8 original families defined; **2 of 8 operational** (Momentum, Pattern & Odds).
**6 of 8 are untrained** because no installation has reached the label gates. Producing
forward forecasts: 8 non-derived candidates (#1, 8–14). The "five challengers" in the
launcher (`ensemble,random_forest,gradient_boosting,lightgbm,xgboost`) are five of the
eight originals, not five extra models. Quote-depth imbalance is listed in
`docs/REMAINING-RESEARCH.md` but has no row in Report 4.

## 3. Actual primary model-selection logic (`prophet/autonomous.py`, `adaptive_entry.py:220-241`)

1. The default primary forecast is `volatility-reference-gap-v1` (formula #9).
2. With an open position, the model that opened it is **pinned** (`OPEN_POSITION_MODEL_LOCK`). Confirmed.
3. Otherwise `Selection.rank` takes the latest ≤100 settled `momentum-v2` $1 / 90 s / HOLD
   lab orders **in the same volatility regime** (threshold 0.001), joins challenger lab
   orders on the same markets (90 s, $1, HOLD, `FORWARD SHADOW`, registered and trained
   before the order), and keeps versions with **≥20 matched markets, positive total P&L and
   positive mean improvement over momentum**. The first one that returns a valid estimate wins.
4. Each switch is logged in `paper_model_switches`; choices in `paper_model_choices`.

Why it never selects (all 64 primary observations say `NO_COMPARABLE_WINNER`): the only
family in that pool with forward lab orders is Pattern & Odds, which lost money
(−$24.09), so the positive-P&L rule correctly rejects it. Six families are untrained.
Formula models #9–#14 are not in the pool at all. **The selector is working as coded; its
pool is empty.**

Design defects (not yet changed; this affects which model trades and needs your approval):
* It compares challengers to **momentum**, the weakest family (−$49.64), not to the
  incumbent Reference Gap model that actually trades the account.
* The evidence is $1 / 90 s / hold, while the primary trades $5 at any second within a
  40–70¢ band with a 10% edge, so the evidence does not match how the model would be used.
* No calibration (Brier/log loss), uncertainty, after-cost comparison against the incumbent,
  or error-correlation check. `docs` mention correlation diagnostics in the Full Excel only.
* `c.version<>'momentum-v2'` is the only exclusion, so a Model Vote lab config could in
  principle enter the pool. It should be excluded explicitly.

## 4. Confirmed defects and status

| # | Defect | Status |
|---|---|---|
| D1 | `app.py` defaults to `data/prophet.sqlite3` and `data/history.sqlite3`; the launcher uses `data/timing/…`. Learning reads only the configured pair, so labels in the other database are invisible. Matches the 15,972→2,038 candle and 47→1 label drop. | Diagnosed; needs your DBs to confirm (`tools/continuity_audit.py`) |
| D2 | Launcher defaults `PROPHET_FRESH_RESEARCH=1`, which skips the 60-market history bootstrap. | Diagnosed; left as designed (documented choice) |
| D3 | Status said "Need 200 labelled examples", but the five ML challengers need **300** and ≥30 per purged split. | **Fixed:** status now reports both gates, the 300-gate shortage and each challenger's outcome (`learning.py`) |
| D4 | **SKIP reasons overwritten.** When the venue empties the book just before the close, the round's last observation (`NO_LIQUIDITY`, model label left at default `momentum-book-v0.2`) replaced the model's real reason. All 31 non-entry rounds in Report 4 show this. | **Fixed** (`unified.py`): a later data-availability block no longer overwrites a model-evaluated reason; regression test fails on the original code, passes now |
| D5 | Selector pool is empty and compares to momentum (see §3). | Open — needs approval |
| D6 | Calibration: `unified.calibrate` shrinks toward `0.5·raw + 0.5·market midpoint` with 30 pseudo-labels; buckets had 0–56 labels, mostly <10. **The "UP estimate" used for the confidence cohorts is therefore mostly market price.** "Confidence ≥70%" largely means "the market already priced it high". | Diagnosed; by design but must be stated in reports |
| D7 | The higher-cost variant changes which trades are taken (e.g. 40–70¢: −$3.72 → +$15.62), so it is not a pure cost-sensitivity test. | Open |
| D8 | "All 10 models agree" requires 10 forecasts; 6 families are untrained, so it can never fill (0/95). | Open — expected until training |
| D9 | Selective-90: 0 calls in 94 markets (opening move too small 54, low momentum 39, missed checkpoint 1). | By design; nothing to evaluate yet |
| ~~D10~~ | ~~Stale-book fills.~~ **Withdrawn.** `book_timestamp` is the venue's last-change time (`transactTime`); `book_checked` is set only after a successful book poll (all early exits return `False`), and entry requires that confirmation to be ≤10 s old at both decision and commit. The 23 fills on books unchanged for 15–30 s are valid. | Verified not a defect |

## 5. Reproduced results (Quick Report 4, `tools/reproduce_primary.py`)

All directive figures reproduce exactly; Report 3 gives identical results.

| Cohort | Trades | Wins | Net P&L | $/staked | Max DD | Win 95% CI |
|---|---|---|---|---|---|---|
| Existing strategy | 62 | 34 | +$13.01 | 0.042 | $29.01 | 0.43–0.67 |
| Hold to settlement | 62 | 39 | +$14.72 | 0.048 | $49.01 | 0.50–0.74 |
| Confidence ≥ 60% | 50 | 28 | +$4.00 | 0.016 | $22.90 | 0.42–0.69 |
| Confidence ≥ 70% | 27 | 18 | +$19.10 | 0.142 | $9.86 | 0.48–0.81 |
| Confidence ≥ 80% | 5 | 4 | +$5.83 | 0.234 | $2.69 | 0.38–0.96 |
| Confidence ≥ 90% | 0 | – | – | – | – | – |
| Entry < 90 s | 43 | 22 | +$12.22 | 0.057 | $18.93 | 0.37–0.65 |
| Entry ≥ 90 s | 19 | 12 | +$0.79 | 0.008 | $16.64 | 0.41–0.81 |

* Early exits: 35 of 62 sold early, −$33.39 actual vs −$31.68 held (−$1.71), but holding's
  drawdown is $49 vs $29.
* +240 s exit vs hold: −$13.47 (standard), −$14.46 (higher costs). Underperformed.
* Gap Agreement table: all five rows match the app's *Forward Research* sheet (checked
  against the sheet's totals; per-trade detail not in the Quick Report).
* High win rate ≠ profit: 90–94¢ band 35/36 wins +$8.85; 95–99¢ 36/36 wins +$6.95.
* Scale: Reference Gap + market agree won 66/94 (70%) at $100 and **lost $153.18**; the same
  rule at $3 lost $5.84. Depth and price impact dominate at larger size.
* ~58 policy/cost rows were evaluated on the same 94 markets. No cohort's interval excludes
  the others; none is promotable on this evidence.

## 6. Tests

| Suite | Before | After |
|---|---|---|
| 0.3.23 full suite (Python 3.13, `.[dev,research]`) | **180 passed, 2 skipped**, 52 s | **183 passed, 2 skipped**, 54 s |
| Skipped | LSTM and neural-reliability tests (no `torch`) | same |
| `tests/` for tools | – | **6 passed** |
| Audit tool on DBs created by the real 0.3.23 app | – | passed |

Not run: browser/dashboard clicks, Excel reconciliation against a live DB, restart/feed
recovery on a Mac, VPS checks, training on real data (no DB).

## 7. Run on your Mac

Continuity audit (read-only, safe while the app runs; leave out missing files):

```bash
python3 tools/continuity_audit.py \
  --store original=data/prophet.sqlite3 --store timing=data/timing/prophet.sqlite3 \
  --history original=data/history.sqlite3 --history timing=data/timing/history.sqlite3 \
  --json continuity-audit.json
```

Cohort reproduction from any Quick Report:

```bash
python3 tools/reproduce_primary.py BTC-Prophet-Quick-Report-4.xlsx --json cohorts.json
```

Installing the patched app: stop the running app, back up the whole project folder, copy
`btc-prophet/prophet/` over the existing `prophet/`, and start from the **same folder** with
the same launcher so `data/timing/` is reused. Rollback: restore the backed-up `prophet/`
folder. Do not do this without reviewing the diff (`git diff a764353 -- btc-prophet`).
