# Replay simulation results — can BTC Prophet reach a 90% win rate profitably?

Data: `BTC-Prophet-Paper-Report.xlsx` (Full audit, SHA-256 `ca2163e6…cfdfd1`), the same run as
Quick Report 4: **94 officially settled markets**, 2026-11-26 06:45–14:35 UTC, 51% UP.
8,985 fresh executable quotes (rejected or stale quotes excluded). Fills use the app's own
`prophet.execution.execute` ($5 budget incl. fees, 25% of displayed top-of-book depth, one
adverse tick, ≥95% of the budget must fill). One entry per market per strategy, held to the
official outcome. **68,850 strategy variants** (signal × start second × minimum confidence ×
price band × minimum edge). Script: `research/replay_sim.py`; raw output:
`research/results-quick-report-4-run.json`.

## 1. Who forecasts best

| Seconds into round | Market price (Brier) | Reference Gap model (Brier) | Direction right: market / model |
|---|---|---|---|
| 0–60 | **0.228** | 0.243 | 60% / 62% |
| 60–120 | **0.182** | 0.197 | 72% / 73% |
| 120–180 | **0.137** | 0.146 | 80% / 79% |
| 180–240 | **0.094** | 0.104 | 88% / 88% |
| 240–300 | **0.054** | 0.062 | 96% / 93% |

Lower Brier is better. **The order book's own price beats the model at every stage.** The
model has not shown information the market does not already price.

The market is also well calibrated. The favourite's price vs how often it won:
50–60¢ → 45%, 60–70¢ → 70%, 70–80¢ → 71%, 80–90¢ → **89%**, 90–99¢ → **99%**.

## 2. What a 90% win rate takes

* Any strategy can reach 90% by **buying the favourite late, at 80–99¢**. 11,342 of the
  68,850 variants hit ≥90% in-sample with ≥20 trades. Their **median return is 8.2¢ per
  dollar staked**, and that is in-sample (overstated).
* At 90¢ a win pays ~10¢ and a loss costs ~90¢. One loss wipes out about nine wins.
  Pre-declared "late favourite" rule (≥240 s, 85–99¢): first half 10/10, +$2.46;
  second half 5/6, **−$4.12**.

## 3. "We know the answers — run it again and again" (experiments A–D)

| Experiment | What it does | Result |
|---|---|---|
| **A. In-sample, answers known** | Pick the best of 68,850 using all 94 outcomes | Best P&L: 35/39 (90%), **+$57.40**. Best win rate: 23/23 (100%), +$33.22 |
| **B. Walk-forward** | Pick the best on markets 1–47, freeze it, run on untouched 48–94 | By P&L: train 38/40 (95%) +$35.15 → **test 28/35 (80%), −$0.39**. By win rate: train 34/34 (100%) +$22.48 → **test 25/28 (89%), +$1.27** |
| **C. Shuffled outcomes** | Scramble which market won and repeat A, 100 times | Best P&L found in pure noise: median **+$61.63**, 95th percentile +$119.69. **55% of the noise runs beat the real best (+$57.40)** |
| **D. Determinism** | Run the whole pipeline 3× | Identical every time (hash `6cf9357c…`) |
| **E. Bootstrap of B's frozen strategy** | Resample its 35 test trades 2,000× | Mean −$0.27; 90% range −$25 to +$23; **49% chance of a loss** |

What this shows:
* **Yes, it picks the same answer every time** (D). That is consistency, not reliability.
* **Tuning on known answers memorises them** (A→B). Win rates fell from 95–100% to 80–89%
  on unseen markets, and profit fell to about zero.
* **The best in-sample profit is no better than luck** (C). When you search 68,850 variants,
  something always looks great. The high *win rates* are real, but they come from market
  calibration (buying favourites), not from model skill.

## 4. Pre-declared strategies (no tuning), first half vs second half

| Strategy | Markets 1–47 | Markets 48–94 |
|---|---|---|
| Reference Gap, 40–70¢, edge ≥10% (≈ current primary rule) | 14/29, −$29.14 | 22/36, +$9.25 |
| Reference Gap, confidence ≥75%, 40–70¢ | 14/22, −$2.23 | 18/28, +$1.81 |
| Late favourite ≥240 s, 85–99¢ | 10/10, +$2.46 | 5/6, −$4.12 |

None is consistently profitable across the two halves.

## 5. How much data is needed (95% Wilson intervals)

| Claim to prove | Trades needed |
|---|---|
| Win rate above 90%, if the true rate is 95% | **127** |
| Win rate above 90%, if the true rate is 92% | **830** |
| Beats an 88¢ break-even, observed 93% | **148** |
| Beats a 60¢ break-even, observed 70% | **88** |
| Beats a 65¢ break-even, observed 75% | **82** |

These are for **one** strategy chosen in advance and tested on **new** markets. Testing many
strategies on the same markets needs more (multiple-comparison correction). Also: 300
labelled markets, enough to train the six untrained ML models, is ~26 hours of continuous
running (288 markets a day). Several days to weeks are needed to cover different volatility
regimes; all 94 markets here come from one eight-hour session.

## 6. Models and scenarios

Do not add more models built on the same three inputs (60 s momentum, 5 min trend,
volatility): they will repeat each other. What is worth adding:
1. **Market price as the baseline every model must beat** (Brier and after-cost P&L). A model
   only earns money when it disagrees with the price and is right.
2. **A "disagreement" scenario:** enter only when the model's probability differs from the
   price by at least X points, at a pre-declared X.
3. **A settlement-average model:** settlement uses the mean of the 60 one-second BRTI values
   before the close. With under 60 s left, part of that average is already fixed. That is
   genuinely new information, but the market may already price it.
4. **Order-book imbalance** already exists as a shadow (Brier 0.192 on 43 forecasts). Keep it.
5. **Fix forecast coverage first:** 144 of 570 fixed-checkpoint forecasts (25%) were missing
   (102 incomplete feature window, 42 missed checkpoint).
