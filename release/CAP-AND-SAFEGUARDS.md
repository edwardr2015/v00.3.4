# Training cap removal and safeguard simulation (94 markets, Paper Report)

## 1. Training caps: what changed

| Limit | Was | Now (default) | Setting |
|---|---|---|---|
| Logistic challenger gate (`learning.py`) | 200 labels | 30 | `PROPHET_MIN_TRAINING_MARKETS` |
| Six ML challengers (`models.py`) | 300 markets | 30 | same |
| Each purged 60/20/20 split | 30 | 5 | `PROPHET_MIN_SPLIT_MARKETS` |
| Logistic fit (`research.fit_challenger`) | 100 examples | 10 | `PROPHET_MIN_FIT_EXAMPLES` |
| Retrain after first success | 12 h | 1 h | `PROPHET_RETRAIN_SECONDS` |
| LSTM | 2,000 | unchanged (PyTorch not installed) | – |

Every model trained on fewer than 300 markets has `training_limits.small_sample = true` in its
registry metadata. Unchanged: chronological purged splits, shadow-only status, and the rule that
a challenger needs ≥20 matched forward markets with positive P&L before it can run the primary
account. Setting the variables back to 300/30/100/43200 restores the old behaviour (tested).

## 2. Does training on fewer markets help? (`research/learning_curve.py`)

Walk-forward on the 89 markets with live-captured model features: each market is predicted only by
a model trained on earlier markets, retrained every 5 markets. A trade is taken when the model's
probability beats the all-in price by 5 points; $5, app fill model, held to settlement.

Same last 29 markets for everyone (Brier: lower is better, coin flip = 0.250):

| Forecaster | Brier | Trades | Wins | P&L |
|---|---|---|---|---|
| **Market price** | **0.227** | 0 | – | $0 |
| Reference Gap (current primary) | 0.242 | 14 | 8 | +$1.60 |
| Fixed momentum | 0.256 | 16 | 4 | −$36.33 |
| Random Forest | 0.254 | 16 | 5 | −$9.86 |
| Ensemble | 0.260 | 14 | 4 | −$26.33 |
| Logistic | 0.268 | 14 | 4 | −$26.34 |
| LightGBM | 0.275 | 20 | 6 | −$41.70 |
| XGBoost | 0.320 | 20 | 5 | −$50.84 |
| Gradient Boosting | 0.350 | 20 | 6 | −$37.44 |

Allowing trading from an earlier start (all markets from N onward):

| Model | Start after 10 | 20 | 30 | 40 | 60 |
|---|---|---|---|---|---|
| Logistic P&L | −$57.78 | −$41.64 | −$32.11 | −$24.13 | −$26.34 |
| Ensemble P&L | −$14.73 | −$5.11 | −$5.63 | −$0.63 | −$26.33 |
| Random Forest P&L | −$11.50 | −$8.42 | −$2.59 | +$10.94 | −$9.86 |

Conclusion: removing the cap lets the models **start** learning, which is required to get any
evidence at all. With 10–90 markets and these three inputs (momentum, trend, volatility), none
forecasts better than a coin flip or the market price, and trading on them early loses more.
Keep them as shadows until forward evidence says otherwise.

## 3. Removing primary safeguards one at a time (`research/safeguards_sim.py`)

Replays the primary rule (`unified.py`) on the 8,985 recorded fresh quotes, with the app's fill
model. Validation: the baseline replay gives 49 trades, 29 wins, +$15.40; the real account had 62
trades, 34 wins, +$13.01.

| Variant | Trades | Win rate (95% CI) | P&L | Max DD | 1st half | 2nd half |
|---|---|---|---|---|---|---|
| A baseline (current rule) | 49 | 59% (45–72) | +$15.40 | $26 | +$15.84 | −$0.43 |
| B no price band | 67 | 46% (35–58) | −$9.43 | $37 | −$6.18 | −$3.25 |
| C min edge 10% → 0% | 72 | 57% (45–68) | **+$25.60** | $17 | +$12.04 | +$13.57 |
| D no edge test at all | 81 | 43% (33–54) | −$28.80 | $49 | −$9.82 | −$18.98 |
| E raw model, no market shrink | 71 | 52% (41–63) | −$2.09 | $31 | −$2.00 | −$0.09 |
| F hold to settlement | 49 | 61% (47–74) | −$9.38 | $44 | −$0.98 | −$8.40 |
| G flips allowed | 49 | 59% | +$15.40 | $26 | no flip ever qualified | |
| H entries before 90 s only | 30 | 57% (39–73) | +$10.10 | $16 | +$14.76 | −$4.66 |
| I entries at/after 90 s only | 39 | 62% (46–75) | +$19.32 | $20 | +$17.54 | +$1.78 |
| J **add** model+market agreement | 40 | **70% (55–82)** | **+$33.58** | **$14** | +$23.17 | +$10.41 |
| K band + edge removed | 94 | 40% (31–51) | −$34.68 | $51 | −$2.67 | −$32.00 |
| L everything removed, hold | 94 | 50% (40–60) | −$23.64 | $66 | −$27.30 | +$3.66 |

* Removing the **price band**, the **edge test** or the **market shrink** lowers the win rate and
  loses money. Removing everything gives a coin flip (50%) and −$23.64.
* Lowering the edge from 10% to 0% (C) is the only removal that helped, and it did so in both halves.
* **Adding** a safeguard (J: model and market must agree) gave the highest win rate and profit,
  positive in both halves.
* Early exits: here holding was $24.78 worse, while in the real account holding was $1.71 better.
  The exit effect is not settled; it depends on which later quotes are observed.
* Twelve variants on the same 94 markets: C and J are candidates for a pre-declared forward test,
  not proven improvements.

Never relaxed: quote freshness, executable depth, one position at a time, official settlement.
2,349 quotes had no usable book; they are excluded, not filled.
