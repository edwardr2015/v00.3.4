# BTC Prophet v0.3.23 research and audit update

See [the 0.3.23 audit and timed-exit notes](docs/0.3.23-research-completion.md) and the [0.3.22 research and verification notes](docs/0.3.22-corrective-research.md) for the synchronized model panel, independent paper comparisons and evidence limits.

## dev12: Selective rule at $50 per trade (paper)

- A second Selective ledger: $1,000 start, $50 per trade, same rule (minute 3–4, all 3 agree, ≤90¢, reversal exit). It never forces a trade: it enters only if at least 95% of the $50 can be bought at ≤90¢ right then, and sells early only if the whole position can be sold. It stops below $50, never adds money and never goes below $0. It shows in the Test accounts table and the Excel sheet "Selective $1000 at $50". Each Selective ledger runs in its own savepoint so one can never change the other.

## dev11: early exit stays on, test accounts, all models on the dashboard

- **Early exit is now the default.** The main account sells early unless `PROPHET_PRIMARY_EXIT=hold` is set. Before dev11 a restart without the setting silently switched to holding (Report 14: 1:58–9:10 PM on Nov 26, −$42.75). The dashboard shows "Early exit: ON/OFF".
- **Per-minute learning for the main model.** Forecasts are remembered once per minute of every round. After 30 settled same-minute forecasts in a confidence bucket, the bot judges a minute-3 call against earlier minute-3 calls instead of minute-0 calls. Until then it behaves exactly as before.
- **Reversal exit shadow.** Exit Shadow Comparison now also scores "sell when the model flips to the other side". It is a comparison only.
- **Three $50 test accounts ($3 trades, never below $0, never topped up, paper only):** main rule without minute-2 entries, main rule at 40–55¢ only, and the 90-second agreement research rule. They are on the dashboard and in the Excel sheet "Test Accounts". The main $1,000 account and the Selective $50 account are unchanged.
- **All models & learning panel.** Every model account, whether it retrains, learns from results or is a fixed rule, and the hourly training score compared with a coin flip.

## 0.3.23 on an existing Mac

Use the verified release ZIP through **Settings → Software updates** with paper decisions paused, or save the accompanying installer and ZIP in Downloads and run `bash "$HOME/Downloads/Install Prophet 000.300.023.command"`. The installer stops an identified Prophet process gracefully if needed, verifies the ZIP, backs up the database, selects 0.3.23 and launches it through the original install. Keep the existing project directory and its database. The scoreboard continues your 0.3.21 unified account. A first unified install starts its ledger at $1,000. Previous paper ledgers and all saved model versions remain available in Research Results and Excel.

The first 0.3.21 launch starts paper simulation automatically if there is no critical lock or frozen experiment. Ordinary later restarts resume the previous decision mode, including an intentional pause. Collection resumes automatically and fresh provider confirmations are required. A historical routine restart flag is cleared automatically. Explicit emergency stops and genuine critical locks still require review. An active older frozen experiment requires review because the protocol fingerprint has changed.

The paper strategy evaluates both directions while an eligible market is open. It starts from Volatility-Adjusted Reference Gap or a previously evaluated model selected by the existing matched forward model ranking. The model stays pinned while a $5 position is open. The estimate is shrunk toward the market midpoint with sparse data and updated from verified, earlier same-model forecasts in similar confidence buckets. This starts with an empty calibration ledger. The default $5 entry requires a 40–70¢ execution price before fees, at least 10% estimated return after costs, and at least 95% of the budget executable under modeled depth. The bot sells only if a later fresh bid can fill the entire position and net proceeds exceed the updated hold estimate by 3% of entry cost. Each closed position has a matched hold result when its official settlement arrives.

The frozen 90-second rule also logs a $100 hold comparison against standard and higher modeled costs. The primary account can try that size only after at least 100 distinct settled qualifying shadow fills, at least 90% wins on those stressed fills, positive results at both cost settings, and stress drawdown no greater than $250. It also requires a fresh current qualifying fill and $100 cash. These thresholds specify a research gate. They do not establish a future win rate. A $100 fill follows the tested hold rule. No live order functionality is present.

Select **Research Results** for individual models, history, matched challenger accuracy and Brier score, and $100 eligibility. Both Excel reports include the unified scoreboard and trades. Full Excel includes the $100 shadow audit.


BTC Prophet is a private, paper-only research application. The unified dashboard shows your adaptive account, eight source models and the derived Model Vote, 25 recent directional predictions, feed health, and the Excel workbook.

## Open on your Mac

1. Install Python 3.11 or newer from python.org. Python 3.14 was smoke-tested on Linux and macOS package resolution was checked. Native Mac operation still needs a test on your Mac.
2. Use `000.300.023.zip`. If upgrading, use the update controls below so your existing data remains attached.
3. Open **Start Prophet.command**. If macOS asks how to open it, open Terminal in that folder and paste `bash "Start Prophet.command"`.
4. Open `http://127.0.0.1:8000`. Choose **LEARN** to record research or **Start adaptive research** for a paper pilot.
5. Use **Pause** and **Resume** to suspend paper decisions. **Emergency stop** requires review before paper trading can resume. Use **Export Excel** to download the report. The new tab starts a Quick Report with up to 500 rows per detail sheet. Click **Full audit workbook** in the report tab for complete history and model evidence. You can keep using the dashboard while either workbook is prepared. A pending full audit does not block a quick report from a separate tab.

Momentum and Pattern & Odds run without extra model packages. Pattern & Odds waits for at least 20 verified outcomes. Logistic Regression and Ensemble need chronological labels. The launcher installs scikit-learn, XGBoost, and LightGBM into the app’s own virtual environment by default. On macOS, LightGBM needs the OpenMP runtime (`brew install libomp` if Homebrew is installed). Run the launcher from Terminal to see dependency errors. Settings and Model details show whether each participant has produced a forward forecast. Archived HMM research can be loaded separately with the `legacy-hmm` extra on supported Python versions. Native Intel Mac startup, market access and performance still need verification.

**Settings → Formal nine-model comparison** shows the preflight. A formal run requires 48 hours of recorded active learning, a fresh eligible market, and all nine participants producing forecasts in the same recent checkpoint. The clock persists across restarts and excludes unattended gaps. The earlier experiment record is archived as unverified. Its orders and balances remain in the ledger. A running comparison from an older build retains its code-change safety lock until reviewed.

The launcher binds to this computer only. Public market-data reads need no private wallet credentials. Optional password protection uses `PROPHET_AUTH_REQUIRED=1 bash "Start Prophet.command"`.

## Existing data

Stop the old application and make a private copy of any surviving application folder before upgrading. The release ZIP contains source and tests. It does not contain account databases, credentials, private research data, or trained artifacts. A deleted Mac database cannot be reconstructed from this ZIP. If your old folder exists, update the program files there and leave `data/` and model artifacts at their existing paths. Use `PROPHET_DB` and `PROPHET_HISTORY_DB` if your installation already points to a different data directory.

The first launch from 000.200.007 preserves its ledger and archives its previous experiment settings. Start a new session from the dashboard. Existing paper positions still settle through verified outcomes.

## Autonomous paper accounts

Select **SIMULATE** to run the existing comparisons and 11 independent autonomous paper accounts. The nine established forecast participants and two formula challengers each have their own $1,000 virtual bankroll and $3 entry budget. The two added hypotheses use market odds, the distance from the opening reference price, volatility, and remaining time. Their weights are fixed for forward research.

Each account evaluates the open market on new observations and chooses one action per evaluation. UP or DOWN opens a position, SKIP declines entry, HOLD keeps an open position, and settlement closes it. Each account can enter a given market once and carry one position at a time. Each funded paper position holds through official settlement. Model versions are pinned for the round. Missing or stale data and shallow depth block fills. A closed or paused round can still be accounted for when official settlement arrives.

All 12 autonomous accounts hold every open paper position to official settlement. Matched Exit Results and Matched Exit Audit compare the same entries with modeled early exits and a single calculated flip. Autonomous Timing Results and Autonomous Timing Audit compare 15, 30, 45, 60, 75, and 90 second checkpoints using the same model and $3 stake. The original leaderboard and fixed-time formal comparison remain available on the same dashboard. Primary adaptive accounts hold to settlement. Shadow flip and take-profit experiments continue. Download Excel for **Autonomous Model Accounts**, **Autonomous Model Decisions**, and **Autonomous Model Trades**. Decision evidence includes both candidate prices, estimated edge, source timestamps and the reason. Settled and cashed-out trades report their matched hold P&L when the official outcome is available.

## Models and execution

The competition shows Momentum, Logistic Regression, Random Forest, Gradient Boosting, Ensemble, XGBoost, LightGBM, Pattern & Odds, and Model Vote. It uses an independently funded $1,000 comparison account for the current version of each participant, with a $1 entry budget at 90 seconds for comparability. The rest of the fixed-time research continues separately. Stored HMM and LSTM research and model versions remain in the archive.

Adaptive paper trading waits for current executable quotes and positive modeled value after costs. Adaptive positions hold to official settlement. Eligible reversals are recorded as unfunded shadow comparisons. Historical funded flips stay in the ledger and settle with their original inputs. Each adaptive account has its own cash and fixed $1, $2 or $3 entry budget. The daily loss cap is off and the safety lock remains in place. The bot can change models automatically **between positions**. A challenger needs at least 20 matched verified trials in the relevant volatility group and better after-cost performance than the reference. When the current model cannot produce a usable forecast while a position is open, the bot waits. Automatic selections are logged. Observer and agreement research stay in the background.

Pattern & Odds blends smoothed verified-outcome transitions, volatility-adjusted reference-price distance, and observed contract odds. It records matching prior sequences, sample sizes, uncertainty, technical indicators, odds, execution assumptions and the official outcome. It waits when data is insufficient. Brier score and log loss measure probability accuracy separately from paper P&L. The archive-only walk-forward check tests the sequence component without presuming quote availability or profitability.

Model Vote counts all eight source forecasts from the same checkpoint. A 5 UP / 3 DOWN split gives a 62.5% UP vote share. This share is recorded separately from the mean model probability. Incomplete votes wait and a tied vote skips the paper entry. Model Vote has its own fixed-time paper accounts and recorded outcomes. Excel’s **Model Vote Trends** compares vote splits across entry checkpoints, volatility, spread and majority changes. Its rows are exploratory and share market outcomes. Automatic adaptive takeover uses the existing trained-challenger rule.

In **SIMULATE**, each newly filled fixed-time or adaptive entry also opens eight matched, unfunded research paths using the same entry. The paths compare holding, net take-profit targets at 1.2× through 1.6×, a probability-based exit, and one reversal funded by modeled sale proceeds. Early exits require enough fresh bid depth to close the whole position. Paths that do not meet their exit conditions hold to official settlement. A skipped entry has no matched exit result. Past orders and the earlier reversal research remain in the ledger.

Use **Matched Exit Results** in Excel to compare each policy with holding by model version, entry time, and stake. The results show whether the policy had enough data to act and how many settled rounds improved or worsened. **Matched Exit Audit** contains the individual entry and exit inputs. These exit comparisons collect forward shadow evidence. Live-money execution remains disabled.

Excel includes model standings, the directional pick record, research forecasts, two-sided unfunded SKIP comparisons, Pattern & Odds research, model switching, repair and downtime history, and the matched $3 **Hold vs Flip Summary** with one extra tick of cost sensitivity. A missing executable quote leaves modeled P&L blank. A model prediction can still be evaluated after settlement when no paper trade was placed.

## Forward viability research

At +45 seconds, the stake comparison tests $3 and $9 on the same book when all eight Model Vote inputs pick the same side. It uses their mean directional probability and requires the existing minimum after-cost edge and fill coverage independently at each size. Excel records misses, wins, losses, fills, and matched P&L in Shadow Stake Results, Shadow Stake Audit, and Paired Shadow Stakes. The same two stake sizes are also compared on actual $3 adaptive entry signals when the model and market midpoint favor the purchased side. These comparisons never debit a paper account or affect its entry and settlement rules.


The Agreement research records paired $3 hold-to-settlement examples at +45, +60 and +90 seconds. **Agreement Price Cap Results** compares the same setup with and without the 85¢ all-in cap. Its audit includes declined entries and fees. These are unfunded comparisons.

**Market Change Audit** compares the latest fresh observation to a strictly earlier observation within 30 seconds of the same market. It logs spreads, traded-contract midpoint, bid and ask depth, volatility, reference move, model votes and feed ages. The frozen alert rules check for spread widening, depth dropping by at least half, and volatility rising by at least 50%. **Timing and Market Conditions** shows a matched what-if result for skipping an alerted entry. This is a directly matched comparison of each entry. It does not recycle bankroll or alter the decision a model made.

**Model Trade Review** compares each resolved autonomous paper trade with its hold result and with the market quote midpoint. It distinguishes wrong direction, entry economics, and value gained or lost by cashing out. Each model’s shadow forecast adjustment uses its own earlier settled entries in the same score range with a minimum of 20 matches. Compare its calibration error with the unchanged forecast on the same new entries. Automated training continues on verified labels in LEARN and SIMULATE. The scheduled retraining interval is now 12 hours when sufficient data is available. Live-money execution remains locked.

## Diagnostics, updates and rollback

Use **Settings → Diagnose** to check database integrity and incidents. **Reconnect feed** is rate limited and requires fresh quotes before simulated fills resume. Critical safety reviews use **Deny**, **Approve**, and **Review complete**. The **Alternative solution** control records a safer review option and keeps the current lock.

For an update, select a trusted release ZIP and paste its published SHA-256 checksum into **Settings → Updates**. Click **Test update** to check the package and manifest, **Back up and stage approved update**, and **Apply approved update on restart**. Quit the terminal session and reopen your original launcher. It verifies the staged files again and opens the same data directory.

**Use installed release on restart** switches back to the original program without restoring old account balances. If the staged program cannot open the dashboard, start the original launcher with `bash "Start Prophet.command" --rollback`. Leave your current data directory in place. Backup snapshots are for recovery review. Do not copy an older database over a newer healthy ledger.

The private GitHub branch is the source of verified packages. The dashboard checks local update history and validates a supplied ZIP. It does not automatically discover private GitHub releases.

## Verification

See **docs/autonomous-release-checklist.md** for the release checks and open gates.

See **docs/competition-repair-verification.md** for this repair and **docs/release-verification.md** for the previous milestone audit. The release candidate can be tested with `python -m pytest -q` after installing `.[dev,research]`. The Linux browser smoke is available as `python scripts/browser_smoke.py` with Chromium and Playwright. The real-trading path remains disabled.

The **Reference Gap 20 Audit** and **Reference Gap 20 Results** sheets test a $20 shadow buy alongside each normal $3 Reference Gap entry. The larger trial requires the model and market midpoint to favor the purchased side, at least 95% modeled deployment of the $20 budget, and at least 10% estimated after-cost return at that size. The report compares both positions at official settlement and records confidence bands, same-side votes and skips. The $20 scenario is an unfunded comparison and never changes a paper account balance.

### Reference Gap agreement forward test

In SIMULATE the new **Reference Gap + Market Agreement** account begins with $1,000 in paper funds. It enters once per round for up to $3 on the first fresh executable quote where Reference Gap and the market midpoint favor the same side and estimated return after costs is at least 3%. If no entry qualifies before closing it records a skip. It holds every fill to official settlement. The original Reference Gap account and LightGBM at +45 seconds are scored on the same verified rounds in **Agreement Forward Rounds** and **Agreement Forward Results**. Archived rounds outside the new account’s observation period are excluded from the comparison.


### $100 pooled consensus shadow

In SIMULATE, one pooled shadow tests up to $100 whenever the eight underlying Vote models, Market Odds Adjustment, and Reference Gap all favor the same side at the same evaluation. Model Vote and Reference Gap + Market Agreement reuse existing forecasts, so they add no extra votes. The mean forecast for the side determines estimated return. Unanimity can occur at any point before closing. The same execution model used elsewhere requires fresh data, at least 95% of the budget fillable, and at least 3% estimated return after costs at that size. A fill holds to official settlement without debiting any account. The **Pooled 100 Results** and **Pooled 100 Round Audit** tabs compare eligible $100 fills with $3 priced at the same quote and log skipped rounds.

### Agreement Comparison

The $100 agreement comparison measures three shadow rules from the same release cohort: all 10 forecast sources agree, all 10 and the market midpoint agree, and Reference Gap and the market midpoint agree. All three wait for a fresh quote that can deploy at least $95 at an estimated after-cost return of at least 3%, and each fills at most once per round. Each uses the same costs and holds to official settlement. The two extra rules are independent comparisons with no effect on paper-account balances, model switching, or previous pooled results. Compare wins, losses, skips, and net P&L in **100 Agreement Comparisons** and trace entries in **100 Agreement Round Audit**.

The primary UP/DOWN card shows the model version that produced the fresh forecast for Adaptive $1. Once that account opens a paper position, the card shows the side and model actually used for entry through the end of that market. The separate $1 Momentum benchmark remains labeled below the signal. A waiting card labels the configured baseline.

Open **Research Results** beside **Settings** to review prediction history, model leaderboards, autonomous accounts, pooled comparisons, the paper ledger, and collector activity. The main dashboard retains the primary signal, market and account overview, and one-click Excel export.

The 90-second shadow records a call only when the Momentum score meets 65% and agrees with a move of at least 12 basis points from the official opening reference. It uses 21 completed 15-second candles and allows five seconds for publication. LEARN records calls, and SIMULATE separately models a $3 hold-to-settlement fill only with a fresh book, enough depth, and at least 3% estimated net return using the earlier historical accuracy estimate. Quick Excel includes a scorecard. Full Excel includes the trade and skip audit.

## Phone remote and updates (dev13)
- Turn on phone access: create an empty file named `phone-remote-on` next to the database (`data/timing/`), then reopen Start Prophet. It asks once for a password and prints the phone address (`http://<your-mac>.local:8000/remote`). Phone and Mac must be on the same Wi-Fi.
- Updates never install on their own. On the phone page press **Check for update** (reads `release/LATEST.json` on GitHub), then **Install now** when ready. It pauses, backs up the database, installs, restarts and resumes paper trading.
- Real-money trading stays locked.
