# Stress test (94 recorded markets, Paper Report 2026-11-26)

Scripts: `research/stress_test.py` (strategies), `research/ledger_stress.py` (software).
Raw output: `research/stress-results.json`, `research/ledger-stress-results.json`.

## 1. Software: the real paper ledger under chaos (`prophet.unified`)

All 8,985 recorded quotes replayed through `unified.observe` and `unified.settle`, with
accounting checks after every market (cash = $1,000 + realised − open cost, ≤1 open position,
≤1 trade per market, cash ≥ 0, settlement pays quantity or 0, entry inside the window).

| Chaos | Ticks | Trades | P&L | Accounting errors | Same trades as clean |
|---|---|---|---|---|---|
| Clean | 8,985 | 53 | +$3.39 | 0 | – |
| Clean, repeated | 8,985 | 53 | +$3.39 | 0 | yes |
| Every tick duplicated | 17,970 | 53 | +$3.39 | 0 | yes |
| Restart mid-run | 8,985 | 53 | +$3.39 | 0 | yes |
| Settlement run three times | 8,985 | 53 | +$3.39 | 0 | yes |
| 20% of markets with no feed | 6,852 | 46 | +$12.88 | 0 | n/a |
| Ticks out of time order | 8,985 | 62 | +$46.71 | 0 | no |
| **30% of books stale (old prices, fresh timestamps)** | 8,985 | **93** | **+$232.86** | 0 | no |

Throughput: about 7,000–10,000 ticks per second (the app needs about 1).

* **Accounting is solid.** No double fills, no lost or created cash, idempotent settlement,
  clean restart.
* **Weakness 1: stale prices produce fake profit.** If the book's prices are old but its
  timestamps look fresh, the bot trades almost every market and "wins" 87%. The app checks
  timestamps, not whether the prices could really be bought. A faster bot would take those
  orders first.
* **Weakness 2: the ledger trusts the clock it is given.** Out-of-order ticks change trades.
  Live, the collector rejects a book older than the last one, so the risk is low, but
  `unified.observe` has no check of its own.

## 2. Strategies under stress

$5 stake unless stated. Each cell: wins/trades, win rate, P&L.

| Scenario | Late Gap (180 s, +10¢) | Late Settle (180 s, +10¢) | Primary-like (40–70¢, hold) | Late favourite (240 s, 85–99¢) |
|---|---|---|---|---|
| Baseline | 31/39 80% **+$33.84** | 28/40 70% +$2.93 | 30/49 61% −$9.38 | 23/24 **96%** +$4.30 |
| Fees +0.02 | 80% +$31.87 | 67% −$12.19 | 66% +$8.03 | 96% +$4.11 |
| Fees +0.05 | 74% +$10.97 | 68% −$8.83 | 66% +$5.33 | 96% +$3.83 |
| Slippage 2 ticks | 73% +$14.08 | 66% −$14.38 | 64% +$2.48 | 100% +$8.49 |
| Slippage 3 ticks | 69% **−$0.67** | 68% −$7.88 | 61% −$7.74 | 100% +$7.22 |
| Latency 2 s | 78% +$24.13 | 68% −$5.18 | 60% −$14.52 | 96% +$3.60 |
| Latency 5 s | 76% +$3.32 | 66% −$21.68 | 60% −$16.04 | 96% +$4.17 |
| Latency 10 s | 76% +$0.49 | 65% −$23.96 | 60% −$18.23 | 95% +$4.23 |
| 25% of quotes missing | 77% +$24.72 | 70% +$3.28 | 62% −$7.24 | 96% +$4.07 |
| 50% of quotes missing | 73% +$7.68 | 69% −$3.99 | 61% −$10.48 | 96% +$4.07 |
| BTC price noise 1 bp | 71% +$21.22 | 71% +$37.87 | 53% −$30.57 | 96% +$4.30 |
| BTC price noise 3 bp | **52% −$18.11** | 48% −$36.18 | 48% −$63.88 | 96% +$4.30 |
| Volatility ×0.5 | 81% +$40.35 | 83% +$46.83 | 62% −$4.03 | 96% +$4.30 |
| Volatility ×2 | **20% −$98.63** | 25% −$93.58 | 55% −$2.05 | 96% +$4.30 |
| Stake $20 | 79% +$76.52 (24 trades) | 68% −$6.42 | 56% −$74.80 | 100% +$24.55 |
| Stake $100 | 78% +$94.16 (9 trades) | 70% −$5.84 | 63% +$93.12 | 100% +$29.21 |
| Start 30 s earlier | 74% +$21.56 | 62% −$21.38 | – | 95% +$4.42 |
| Start 30 s later | 64% **−$15.03** | 63% −$25.53 | – | 100% +$3.50 |
| Edge −5¢ | 79% +$29.40 | 75% +$14.59 | 65% +$17.68 | – |
| Edge +5¢ | 70% +$11.44 | 66% +$1.13 | 63% +$8.06 | – |
| **Realistic combined: fees +0.02, 2-tick slip, 2 s latency** | **65% −$10.04** | 66% −$12.25 | 64% +$2.30 | **100% +$7.70** |

Robustness checks (baseline settings):

| | Late Gap | Late Settle | Primary-like | Late favourite |
|---|---|---|---|---|
| Market bootstrap, 90% range | −$3.74 to +$71.24 | −$34 to +$42 | −$55 to +$34 | −$6.33 to +$11.40 |
| Chance of a loss | 7% | 46% | 64% | 22% |
| P&L without its best 5 trades | +$2.90 | −$24.62 | −$31.92 | +$0.50 |
| P&L by quarter of the day | +19.8 / +0.9 / +6.5 / +6.6 | mixed | mixed | +2.7 / +2.8 / −3.1 / +2.0 |
| Shuffled outcomes doing as well | **18%** | 57% | 13% | 0% |

### What breaks it

1. **Execution speed and costs.** Late Gap survives each cost alone but **fails the realistic
   combination** (−$10.04). Its edge shrinks with every second of delay: +$24 at 2 s, +$3 at
   5 s, +$0.49 at 10 s. That fits the research: the late edge is a speed edge, and faster bots
   compete for it.
2. **The BTC reference price.** 3 basis points of error (about $18 on $60,000) turns Late Gap
   from +$34 to −$18. **This is where the feed matters most.**
3. **The volatility estimate.** Doubling it collapses Late Gap to 20% wins. The volatility
   estimate is a single point of failure.
4. **Timing.** Starting 30 s later loses money; starting earlier still makes money. The
   180-second start is near an edge.
5. **Luck.** 18% of shuffled-outcome runs did as well as Late Gap, so it is not yet
   statistically convincing. Its profit leans on a few big wins (+$2.90 without the best 5).

### 90–95% win rate

The late-favourite rule wins **95–100% under every stress**, including the combined one. That
win rate is real: 0% of shuffled runs matched it. But it earns **$0.18 per $5 trade**, with a 22%
chance of losing money over a day like this one. **A 95% win rate is reachable now. Profit at 95%
is not**, unless the bot gets an information or speed advantage, which no data here shows.

## 3. The Excel export: what is needed and what is not

The 21.6 MB Paper Report has 120 sheets. **28 are empty.** One sheet, *Autonomous Model
Decisions* (147,299 rows), is most of the file.

**Needed for the 90% work (keep):** UP DOWN History (outcomes), Forward Quote Audit (the
executable quotes everything here was built on), Unified Trades and Observations, Forward
Research, Forward Fill Audit, Challenger Outcomes, Synchronized Forecasts, Market Change Audit,
Independent Model Scores, Forecast Reliability, Model Error Correlation, Bug Log, Reliability
and Downtime.

**Not needed for this:** the 28 empty sheets (Roulette Pick History, Smart Flip Research/Actions,
Adaptive Selections/Decisions/Market Windows/Labeled Observations, Later Entry Comparison, Hold vs
Flip Rounds, Paper/Unfunded Flips, Trade History, Skipped Markets, Learning History, WIN LOSS
History, Daily Performance and others), plus the legacy Observer/Agreement ledgers
(Agreement Round Audit, Observer Account Ledger: 2,280 rows each), Sequence Analysis and
Transition Probabilities. *Autonomous Model Decisions* could be cut to one row per model per
market without losing anything this research used.

**Missing, and needed to get closer to 90% with profit:**
1. **Quotes when the book is one-sided.** 67% of last-minute observations are dropped as
   "book unavailable" with no prices recorded. That is exactly where the late edge lives.
2. **Depth beyond the best price.** Only the top level is exported, so $20+ stakes can't be
   tested properly.
3. **Every BRTI one-second value** within each market, to compute the settlement average exactly
   and to measure reference-price error (weakness 2).
4. **Kraken/Coinbase spot prices with timestamps.** The app collects them but doesn't export
   them. Needed to measure whether the market lags (the speed edge).
5. **More days.** One 8-hour session isn't enough; multiple days and volatility regimes are.
