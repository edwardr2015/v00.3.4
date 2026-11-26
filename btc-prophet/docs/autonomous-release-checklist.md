# v0.3.6 autonomous paper research checklist

The candidate extends the existing dashboard and ledger. Use **SIMULATE** to run the autonomous accounts. The Mac onboarding and forward evidence gates are below.

| Check | Status | How it is verified |
|---|---|---|
| Preserve prior ledgers and controls | Automated gate | Start the new build with a copy of a v0.3.5 fixture and compare its previous orders and balances.|
| Independent model decisions | Implemented | Nine existing forecast participants and two formula challengers have separate accounts. Each can choose UP, DOWN, HOLD, SKIP or CASH OUT from its own forecast, current book and position state. |
| Entry and exit at separate times | Implemented | An account chooses on an eligible observation anywhere in the market window. It can enter once per market and may hold or cash out. The primary comparison holds to settlement. |
| Credible modeled fills | Regression gate | Freshness, finite probabilities, spread, fees, slippage, depth, post-entry quote confirmation, market close, settlement verification, and one-open-position rules. |
| Excel traceability | Regression gate | Three autonomous sheets show attempts, inputs, account outcomes, and matched hold P&L. Existing Model Vote and exit sheets remain available. |
| Responsive dashboard and all controls | Browser gate | Chromium desktop and phone viewport flows cover sign-in, modes, emergency review, feed controls, chart controls, model details, prediction tiles, export, update and rollback. |
| Workload durability | Stress gate | Separate synthetic tests cover 1,000 rounds and competing autonomous evaluations, repeated engine starts and stops, and dashboard/control traffic. |
| Model readiness | Isolated inference gate | Reopen trained artifacts from verified archived outcomes and confirm all 11 participants can emit a usable forecast on a synthetic decision-time snapshot. |
| Market data connectivity | Spot-check gate | Read-only BRTI, Polymarket US book/discovery and Kraken observations. |
| Native Mac and Safari | Open | Run the fixed package on the actual Mac. Installation time depends on local dependencies. |
| 24-hour forward paper evidence | Open | Begin after the host connects and continue for 24 hours of recorded coverage. A system interruption can extend the elapsed time. |
| Formal nine-model comparison | Open | The established fixed-time comparison needs 48 hours of recorded active learning plus model/market preflight, followed by its seven-day run. |
| Exact audit of the six reported losses | Open | The original workbook or ledger is needed to reconstruct those specific trades. |

The two added rules are fixed formula hypotheses for forward research. The release may report decisions and historical comparisons as soon as enough data is available. Profitability and forward calibration require new observations. No real-money execution is available.
