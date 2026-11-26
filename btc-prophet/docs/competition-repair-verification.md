# BTC Prophet v0.3.4 competition repair

Baseline is the published private 000.300.003 source. The development candidate keeps the single navy dashboard, existing artwork, research, balances and original paper transactions. Real-money execution remains locked.

## Incident audit

| Issue | Reproduction and expected result |
|---|---|
| Starting a pilot also started a formal seven-day experiment | Starting with an empty database reproduces the old behavior. The new pilot is allowed and creates no formal comparison. A separate formal control rejects missing readiness, persists the 48-hour learning clock, and pins the eight comparison versions after a joint checkpoint. |
| Adaptive reversals changed the primary paper ledger | Find two economically executable quote sets around a probability reversal. The regression checks stale or shallow books, the cutoff, and concurrent attempts. The viable reversal is recorded once as unfunded, with no second debit. Historical committed flips remain in their original table and still settle on the position held. |
| A newly trained model could use an earlier observation while processing a delayed checkpoint | Inject a model trained after a fixed-time capture. Inference from this version is excluded from that capture. Healthy independent models still record their timestamped forecasts, even if execution is skipped. |
| Replay settlement checked the window end without checking when the official result became known | Stage a delayed official result and advance the test clock. Exposure remains open until both the closing and verification timestamps have arrived. Reprocessing the outcome makes no second credit. |
| Missing legacy dependency could block installing unrelated competition models | On Python 3.14 the pinned HMM package lacks a matching macOS wheel, while each required competition package has one. HMM is separated as a legacy extra and the launcher tries the required libraries independently if grouped installation fails. |
| Adaptive open positions previously exposed cost equity only | Reconcile current cash and the net sale value of an executable book. Market equity and unrealized P&L are blank when a complete sale cannot be modeled from fresh depth. No position is sold to calculate this mark. |

The core safety and chronology repairs are logged once in SQLite operational incidents and repair history. Excel includes those records, prior experiment history, current readiness, shadow reversals, account equity and traceable quote, fee and settlement inputs.

## Report-3(3) review

The historical decision audit is **pending**. The owner summarized six losses across two markets, about $13.08 in combined paper losses, and an approximately $4.95 hold-versus-flip difference. The original workbook and ledger were unavailable. The summary cannot establish the historical fault, model timestamp, price, size, or execution assumptions.

Two independent synthetic regressions reproduce the decision patterns. A 40% UP forecast can buy UP when executable UP odds produce better after-cost expected value than DOWN. A profitable DOWN hold can have a losing UP reversal counterfactual. The latter never debits the paper account in this release. The regression amounts are intentionally unrelated to the historical report.

## Models and data

The isolated training copy used the existing archive of 8,588 Polymarket US rounds labeled with verified official BRTI settlement values and historical observations. Seven artifacts trained with chronological holdouts. Momentum, Logistic Regression, Random Forest, Gradient Boosting, Ensemble, XGBoost, LightGBM and Pattern & Odds each produced a timestamped UP/DOWN pair in one synthetic decision-time market. Eight separate $1,000 comparison ledgers reconciled after entry. Pattern & Odds used prior official outcomes and an explicitly synthetic test book to exercise suffix sequences, Markov transitions, streaks, volatility-adjusted reference distance and contract odds. Separate tests injected each of the eight model failures and verified that the other seven remained operational. This proves the research pipeline in an isolated environment. It does not estimate forward profits.

The public, read-only checks on November 24, 2026 confirmed CF Benchmarks BRTI observations through Polymarket US, Kraken BTC/USD spot, current five-minute market discovery and order books. Across the spot checks discovery advanced from the 17:15 UTC market to 17:20 UTC. Three prior outcomes reconciled against the official BRTI opening and closing values. Two used the settlement endpoint and one used the resolved side and BRTI metadata fallback. Delayed, unavailable, stale, market-close and emergency fill paths used synthetic fixtures. No account credentials or real execution endpoints were used.

## Test evidence

| Workload | Result |
|---|---|
| Full Linux Python 3.12 suite | 139 passed, 0 failed, 0 skipped on the full retest. Dependencies included the archived HMM and neural test libraries. |
| Linux Python 3.14.10 | Full competition package installed and imported. 18 targeted regressions passed including all seven artifact fits and the eight model failure scenarios. |
| Desktop and mobile browser | Chromium at 1440×1100 and 390×844. Zero uncaught JavaScript errors or horizontal overflow. See the [control matrix](competition-repair-controls.csv). |
| Operational load | 500 dashboard requests, 100 Start/Pause/Resume sequences, 50 engine restarts and 1,000 competing evaluations using 12 workers. Three unique adaptive orders. Emergency and spreadsheet export passed. |
| 1,000 five-minute stress rounds | 2,250 settled virtual trades, 450 unfunded reversals, zero funded reversals, 750 final skips, 750 compared $3 rounds. SQLite backup, abrupt interruption, balances, and Excel reconciliation passed. |
| Database contention | Writer rollback and competing debit regression passed. Emergency during an in-flight evaluation prevented any debit or order. Delayed and duplicate settlement left the ledger consistent. |
| macOS Python 3.14 | Wheel-only resolution for Intel and Apple Silicon passed. Native installation, Safari, startup/shutdown, network, paper execution, Excel, CPU and memory remain unverified. |

Linux durations and peak child RSS are recorded in the release test evidence. An initial suite exposed test-fixture assumptions about formal start, settlement time, and incident counts. Those were corrected and the full suite rerun. The updated browser suite also corrected locators and checked the new formal-control rejection and authorization.

## Remaining gates

- Deployment is a development candidate. Actual macOS/Python 3.14 execution and the 24-hour prospective hold-versus-flip shadow run remain pending. Keep automatic adaptive flips disabled.
- Review the original Report-3(3) workbook or private ledger when the owner can access it. No estimate of the historical fills or profitability can be verified from the summary alone.
- Fresh Mac installs need eligible observations and training history. Momentum can run first. Pattern & Odds waits for at least 20 verified outcomes. The historical training data and private model artifacts are withheld from the code package.
- The uploaded 0.3.3 dashboard included a money-bag icon. No Monopoly Man asset was present in that archive.
