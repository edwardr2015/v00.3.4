# How the primary paper account decides (0.3.23, verified in source)

Runs on every fresh observation (about once a second) during each five-minute round.
Files: `prophet/adaptive_entry.py` (`evaluate`), `prophet/autonomous.py` (`Selection`),
`prophet/unified.py` (`observe`, `calibrate`, `candidate`).

## 1. Can it look at this round at all? (any "no" → SKIP/WAIT with that reason)

1. A valid five-minute BTC market is open (`validate_market`).
2. Collector not paused; BRTI reference and order book present and not from the future.
3. Reference ≤ the reference age limit; market poll confirmed ≤ 10 s ago (`PROVIDER_ERROR_*`, `STALE_*`).
4. Book belongs to this market and is open (`WAITING_FOR_OPEN_BOOK`).
5. Book has both bids and asks (`NO_LIQUIDITY`). This is usually what happens near the close.
6. Market execution terms are valid (`INVALID_EXECUTION_TERMS`).
7. No paper account is halted for review.

## 2. Which model speaks? (changed in 0.3.23-dev)

1. **Open position → the model that opened it is pinned** (`OPEN_POSITION_MODEL_LOCK`).
2. Otherwise `Selection.rank_incumbent` compares every model's own paper account against the
   **incumbent, Volatility-Adjusted Reference Gap**, on the same settled markets, held to settlement
   (latest 100). A challenger is chosen only if, on ≥20 matched markets:
   - its total P&L is positive;
   - its average improvement over the incumbent is larger than one standard error;
   - it picks a different side from the incumbent on more than 10% of markets (not a copy);
   - its Brier score is no worse than the incumbent's + 0.01.
   Formula models (Market Odds Adjustment) and trained ML models are eligible. Model Vote and
   Reference Gap + Market Agreement are never selectable. The best by average improvement wins.
3. Otherwise the incumbent trades (`NO_COMPARABLE_WINNER`).

Every choice and its evidence (matched markets, P&L, improvement, agreement, Brier) is logged in
`paper_model_choices` and exported as **Model Selection Evidence**. The older adaptive accounts
still use their original momentum comparison.

## 3. Calibrate the forecast (`calibrate`)

Probability = (settled hits in the same 10%-confidence bucket + 30 × prior) / (labels + 30),
where prior = ½ model + ½ book midpoint. With few labels this is **mostly the market price**.

## 4. Decide: ENTER, EXIT, HOLD or SKIP (`unified.observe`)

| State | Rule | Action |
|---|---|---|
| No position, 90–95 s in, Selective-90 has a CALL and a $100 large-stake trial passes, cash ≥ $100 | premium path | ENTER $100 |
| No position | for UP and DOWN: fill $5 at the book; average price 40–70¢, all-in ≤ 85¢, EV ≥ 10% of cost; choose the side with the higher EV | ENTER $5 |
| No position, neither side qualifies | reasons recorded per side (`PRICE_OUTSIDE_BAND`, `EDGE_TOO_SMALL`, depth) | WAIT; at the close this becomes SKIP |
| Another market's position still open | | SKIP (`PRIOR_POSITION_OPEN`) |
| Position open | **hold to official settlement** (default). The old early exit and a 50% stop-loss are recorded as shadows only (`unified_exit_shadows`, sheet **Exit Shadow Comparison**). `PROPHET_PRIMARY_EXIT=probability` restores the early exit. | HOLD |
| UP vs DOWN | the side with the larger expected value, not simply the side above 50% | – |
| **Flip** | **not available in the primary account.** Flips exist only as unfunded shadows in the older adaptive accounts. | – |

One open primary position at a time; one entry per market.

## 5. Does it learn?

| Part | Learns? |
|---|---|
| Reference Gap (current decision-maker) | **No.** A fixed formula. |
| Calibration (step 3) | Yes, slowly: a per-bucket hit rate from settled rounds |
| Model selection (step 2) | Could switch models after 20 matched forward wins; the pool has been empty |
| Pattern & Odds | Rolling outcome frequencies |
| Logistic, RF, GB, XGBoost, LightGBM, Ensemble | **Were never trained** (200/300/100 caps). With the new limits they train after 30 settled markets and retrain hourly, as shadows |
