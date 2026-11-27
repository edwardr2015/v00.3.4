# 200 options to test for BTC Prophet

**Data:** ✅ testable now on recorded data · 🟡 needs the app to record more (order-book depth,
every BRTI second, trade prints, one-sided books) · 🔴 needs a new outside feed · 📅 needs several
days of data.

Already tested and kept are marked **(tested)**. Every option must be judged walk-forward and then
pass on new days it has never seen; see "How to test 200 options without fooling ourselves" at the end.

## A. BTC price path this round (20)
| # | Option | Idea | Data |
|---|---|---|---|
| 1 | Gap + speed **(tested: best in last minute)** | Project current BTC speed to the close | ✅ |
| 2 | Gap + acceleration | Is the move speeding up or fading? | ✅ |
| 3 | Multi-speed drift | Blend 5 s, 15 s, 60 s trends | ✅ |
| 4 | Damped drift | Speed projected with decay (moves fade) | ✅ |
| 5 | Mean reversion (OU) | Small gaps tend to snap back | ✅ |
| 6 | Jump detector | A big 1–2 s move switches to momentum mode | ✅ |
| 7 | Range position | Price location inside this round's high–low | ✅ |
| 8 | Trendiness (Hurst) | Trending versus choppy round | ✅ |
| 9 | Round-number pull | BTC stalls near $100 / $50 levels | ✅ |
| 10 | Breakout of the first minute's range | Direction of the first-minute breakout | ✅ |
| 11 | Time above the open | Share of the round spent above the opening price | ✅ |
| 12 | Max adverse excursion | How far it went against the current side | ✅ |
| 13 | Crossing count | How many times price crossed the open | ✅ |
| 14 | Last-30 s slope | Linear trend of the last 30 seconds only | ✅ |
| 15 | Volatility-normalised speed | Speed divided by volatility | ✅ |
| 16 | Momentum exhaustion | Big move followed by slowing = reversal risk | ✅ |
| 17 | Gap persistence | How long the gap has kept the same sign | ✅ |
| 18 | Previous round's closing move | Momentum carried across rounds | ✅ |
| 19 | 15-minute trend | BTC trend over the last three rounds | ✅ |
| 20 | 1-hour trend | Bigger-picture direction | ✅ |

## B. Volatility (15)
| # | Option | Idea | Data |
|---|---|---|---|
| 21 | Real-volatility gap **(tested: worse)** | Volatility from this round's own moves | ✅ |
| 22 | Volatility × 1.5 calibration **(tested: helps a little)** | Fix the gap model's overconfidence | ✅ |
| 23 | HAR volatility | Daily/hourly/minute volatility mix (Bitcoin standard) | ✅ |
| 24 | GARCH(1,1) | Volatility that reacts to shocks | ✅ |
| 25 | EWMA volatility, fast | 30 s half-life | ✅ |
| 26 | EWMA volatility, slow | 10 min half-life | ✅ |
| 27 | Fast/slow volatility ratio | Volatility rising or falling | ✅ |
| 28 | High–low (Parkinson) | Uses candle ranges | ✅ |
| 29 | Garman–Klass | Open-high-low-close volatility | ✅ |
| 30 | Volatility regime switch | Separate calm and wild forecasts | ✅ |
| 31 | Jump-robust volatility | Ignore single spikes | ✅ |
| 32 | Student-t tails **(exists as shadow)** | Fatter tails than normal | ✅ |
| 33 | Volatility-of-volatility | Unstable markets need wider ranges | ✅ |
| 34 | Time-of-day volatility profile | US open, Asia, weekends | 📅 |
| 35 | Implied volatility from the market price | Back out what volatility the market is pricing | ✅ |

## C. Settlement mechanics (10)
| # | Option | Idea | Data |
|---|---|---|---|
| 36 | Settlement average (exists) | Close = mean of 60 one-second values | ✅ |
| 37 | Exact settlement average | Using every BRTI second in the window | 🟡 |
| 38 | Settlement average + speed | For the last 60 s | 🟡 |
| 39 | Opening-average noise | The opening price is also an average | 🟡 |
| 40 | Locked-in share | How much of the closing average is already fixed | 🟡 |
| 41 | Tie rule (equal = UP) | Tiny edge when the gap is near zero | ✅ |
| 42 | Cent-rounding effect | Both averages are rounded to cents | ✅ |
| 43 | BRTI-versus-spot gap | The index lags constituent exchanges | 🔴 |
| 44 | Stale-constituent effect | An exchange dropping out shifts the index | 🔴 |
| 45 | Settlement-delay rule | Data gaps defer settlement | ✅ |

## D. Polymarket order book (22)
| # | Option | Idea | Data |
|---|---|---|---|
| 46 | **Stale-book detector** | BTC moved but the book didn't reprice | ✅ |
| 47 | Order-flow imbalance | Size changes at best bid/ask between quotes | ✅ |
| 48 | Book pressure **(tested: ≈ market)** | Bid vs ask size at the top | ✅ |
| 49 | Spread signal | Wide spread = market makers unsure | ✅ |
| 50 | Spread change | Spread widening fast | ✅ |
| 51 | Market price speed | Mid moving fast in one direction | ✅ |
| 52 | Market price acceleration | Mid speeding up | ✅ |
| 53 | Market momentum **(tested: mixed)** | Mid pushed in its recent direction | ✅ |
| 54 | Book-versus-BTC lag | Seconds between a BTC move and the book's response | ✅ |
| 55 | Quote refresh rate | How often the book changes | ✅ |
| 56 | Depth collapse | One side suddenly thin | ✅ |
| 57 | One-sided book signal | Only one side quoted = near-certain outcome | 🟡 |
| 58 | Deep microprice | Pressure using several levels | 🟡 |
| 59 | Depth asymmetry | Thin vs thick side beyond the best price | 🟡 |
| 60 | Hidden-liquidity estimate | Fills larger than displayed size | 🟡 |
| 61 | Trade-print momentum | What people actually bought | 🟡 |
| 62 | Trade-size signal | Large trades = informed traders | 🟡 |
| 63 | Aggressor imbalance | Buyer- vs seller-initiated trades | 🟡 |
| 64 | Cancel rate | Market makers pulling quotes | 🟡 |
| 65 | Queue position estimate | Chance a limit order fills | 🟡 |
| 66 | Price-level clustering | Liquidity piles at round cents | ✅ |
| 67 | Book reset at the open | The first minute's book behaviour | ✅ |

## E. The market price itself (15)
| # | Option | Idea | Data |
|---|---|---|---|
| 68 | Market price baseline (exists) | The price is a forecast | ✅ |
| 69 | Calibrated market | Correct the favourite–longshot bias by minute | ✅ |
| 70 | Calibrated market by price band | Bias differs at 60¢ vs 90¢ | ✅ |
| 71 | Model-vs-market disagreement | Trade only when they differ by X | ✅ |
| 72 | Agreement filter **(tested: helps)** | Trade only when they agree | ✅ |
| 73 | Market overreaction | Mid jumps more than BTC justifies | ✅ |
| 74 | Market underreaction | Mid lags a clear BTC move | ✅ |
| 75 | Market-implied gap | Convert the price to an implied BTC move | ✅ |
| 76 | Implied-vs-actual gap | Difference = mispricing | ✅ |
| 77 | Price anchoring at 50¢ | Prices stick near 50¢ early | ✅ |
| 78 | Late-round favourite bias | Favourites underpriced in minutes 3–4 | ✅ |
| 79 | Longshot fade | Never buy below 20¢ | ✅ |
| 80 | Midpoint vs last trade | Divergence signal | 🟡 |
| 81 | 15-minute market cross-check | Overlapping Polymarket BTC market | 🔴 |
| 82 | Other-venue price (e.g., Kalshi) | Cross-venue disagreement | 🔴 |

## F. Outside markets and data (18)
| # | Option | Idea | Data |
|---|---|---|---|
| 83 | **Coinbase lead** | Coinbase moves before BRTI | 🔴 (collected, not exported) |
| 84 | Kraken lead | Kraken price first | 🔴 (collected, not exported) |
| 85 | Binance.US lead | Most-cited price-discovery venue | 🔴 |
| 86 | Exchange divergence | Coinbase and Kraken disagree | 🔴 |
| 87 | Spot volume surge | Volume spike = real move | 🔴 |
| 88 | Futures basis | Futures vs spot premium | 🔴 |
| 89 | Funding rate | Leveraged positioning | 🔴 |
| 90 | Open-interest change | New money entering | 🔴 |
| 91 | Liquidation feed | Forced selling or buying cascades | 🔴 |
| 92 | ETH co-movement | ETH moves with or before BTC | 🔴 |
| 93 | SOL co-movement | High-beta alt as a lead | 🔴 |
| 94 | Stablecoin flow | USDT/USDC premium | 🔴 |
| 95 | Options volatility (DVOL) | Options traders' expectations | 🔴 |
| 96 | Stock index futures | US equities risk-on/off | 🔴 |
| 97 | Dollar index | Macro pressure | 🔴 |
| 98 | Economic-calendar flag | Avoid CPI/FOMC minutes | 🔴 |
| 99 | News-headline flag | Breaking-news volatility | 🔴 |
| 100 | Social-sentiment burst | Sudden attention spikes | 🔴 |

## G. Time and seasonality (10)
| # | Option | Idea | Data |
|---|---|---|---|
| 101 | Hour-of-day UP rate | Some hours lean UP | 📅 |
| 102 | Day-of-week effect | Weekend vs weekday | 📅 |
| 103 | US-open window | 13:30–15:00 UTC behaviour | 📅 |
| 104 | Asia-session window | Overnight US behaviour | 📅 |
| 105 | Top-of-hour effect | Rounds starting at :00 | 📅 |
| 106 | Funding-time effect | Around 00/08/16 UTC | 📅 |
| 107 | Holiday / low-liquidity days | Thin markets | 📅 |
| 108 | Month-end effect | Rebalancing flows | 📅 |
| 109 | Option-expiry days | Pinning near strikes | 📅 |
| 110 | Previous-round outcome | Does UP follow UP? | ✅ |

## H. Machine learning models (20)
| # | Option | Idea | Data |
|---|---|---|---|
| 111 | Blended learner **(tested: best new all-rounder)** | Logistic mix of gap, market, timing, momentum | ✅ |
| 112 | Boosted learner **(tested: worse)** | Trees on the same features | ✅ |
| 113 | **Online learner** | Updates after every settled round | ✅ |
| 114 | Per-minute models | A separate model for each minute of the round | ✅ |
| 115 | Ridge with interactions | Gap × time, gap × volatility | ✅ |
| 116 | Monotonic boosted trees | Forced to respect "bigger gap = more UP" | ✅ |
| 117 | Random forest on rich features | More inputs than the current RF | ✅ |
| 118 | k-nearest rounds | Find similar past rounds by path shape | ✅ |
| 119 | History table **(tested: weak)** | P(UP given gap and minute) | ✅ |
| 120 | Isotonic calibration of any model | Fix overconfidence | ✅ |
| 121 | Bayesian logistic | Uncertainty-aware weights | ✅ |
| 122 | Small neural net (MLP) | Non-linear mix | 📅 |
| 123 | Sequence model (GRU) | Learns from the whole price path | 📅 |
| 124 | Temporal CNN | Pattern shapes in the path | 📅 |
| 125 | Survival model | Chance the gap flips before the close | ✅ |
| 126 | Quantile model | Forecast range, not just a point | ✅ |
| 127 | Conformal bands | Guaranteed-coverage ranges | ✅ |
| 128 | Meta-labelling | A second model decides if the first is right | ✅ |
| 129 | Feature-selected sparse model | Lasso picks the useful inputs | ✅ |
| 130 | Distilled ensemble | One small model copying the best mix | ✅ |

## I. Combining and choosing models (15)
| # | Option | Idea | Data |
|---|---|---|---|
| 131 | Best-of weighting | Weight by recent accuracy | ✅ |
| 132 | Stacking | A model that learns how to combine models | ✅ |
| 133 | Median of models | Robust to one bad model | ✅ |
| 134 | Agreement count | Trade when N of M agree | ✅ |
| 135 | Bayesian model averaging | Weights from evidence | ✅ |
| 136 | Regime-based switching | Different model per regime | ✅ |
| 137 | Rule-picking bandit (Thompson) | Shift toward winning rules | ✅ |
| 138 | Epsilon-greedy rule choice | Simpler bandit | ✅ |
| 139 | Correlation-pruned vote | Drop models that copy each other | ✅ |
| 140 | Selection under the primary's rule | The proper selection fix | ✅ |
| 141 | Recency-weighted selection | Recent rounds count more | ✅ |
| 142 | Hysteresis switching | Switch only on a clear margin | ✅ |
| 143 | Champion–challenger with holdout | Formal promotion test | ✅ |
| 144 | Disagreement-based skip | Skip when the top models disagree | ✅ |
| 145 | Confidence-weighted vote | Votes count by calibrated confidence | ✅ |

## J. When to enter (15)
| # | Option | Idea | Data |
|---|---|---|---|
| 146 | Minute 3–4 follow **(tested: strong)** | Enter 180–240 s | ✅ |
| 147 | Minute 3–4 + agree + ≤90¢ **(tested: best)** | The current best rule | ✅ |
| 148 | **Best-entry-second model** | Predict the best moment instead of a fixed window | ✅ |
| 149 | Gap-threshold entry | Enter once the gap exceeds X standard deviations | ✅ |
| 150 | Probability-threshold entry | Enter once the model passes X% | ✅ |
| 151 | Price-threshold entry | Enter when the favourite costs 70–85¢ | ✅ |
| 152 | First-pullback entry | Wait for a small dip in the favourite's price | ✅ |
| 153 | Breakout entry | Enter on a fresh high/low after minute 2 | ✅ |
| 154 | Two-stage entry | Small early signal, confirm at minute 3 | ✅ |
| 155 | Latest-safe entry | As late as possible while depth remains | 🟡 |
| 156 | Volatility-timed entry | Earlier in calm markets, later in wild ones | ✅ |
| 157 | Book-lag entry | Enter when the book lags BTC | ✅ |
| 158 | 30–60 s entry **(tested: failed in replay)** | Early window | ✅ |
| 159 | Market-confirmation entry | Enter after the market moves your way | ✅ |
| 160 | Skip-first-minute rule | Never trade before 60 s | ✅ |

## K. When to exit (10)
| # | Option | Idea | Data |
|---|---|---|---|
| 161 | Hold to the end (current) | Never sell | ✅ |
| 162 | Probability exit **(live shadow: winning)** | Sell when the bid beats the value | ✅ |
| 163 | **Learned exit** | A model predicts when selling beats holding | ✅ |
| 164 | Take-profit 1.2–1.6× **(tested: worse)** | Fixed profit target | ✅ |
| 165 | 50% stop-loss **(live shadow: worse)** | Cut losses | ✅ |
| 166 | Trailing exit | Sell after giving back X from the peak | ✅ |
| 167 | Time exit at 4:30 | Sell before the last 30 s | ✅ |
| 168 | Reversal exit | Sell when the model flips side | ✅ |
| 169 | Partial exit | Sell half at a target | 🟡 |
| 170 | Flip | Sell and buy the other side **(tested: never triggered)** | ✅ |

## L. When to sit out (10)
| # | Option | Idea | Data |
|---|---|---|---|
| 171 | Wide-spread skip | Skip if the spread is over 3¢ | ✅ |
| 172 | Thin-book skip | Skip if depth is too low for $3 | ✅ |
| 173 | High-volatility skip | Skip wild rounds | ✅ |
| 174 | Low-volatility skip | Skip dead rounds | ✅ |
| 175 | Near-zero-gap skip | Skip when BTC is right at the open | ✅ |
| 176 | Model-disagreement skip | Skip when the top models disagree | ✅ |
| 177 | Stale-data skip (exists) | Never trade stale quotes | ✅ |
| 178 | Losing-streak pause | Pause after N losses in a row | ✅ |
| 179 | **Skip predictor** | A model learns which rounds to avoid | ✅ |
| 180 | News/event skip | Avoid scheduled events | 🔴 |

## M. Risk rules at $50 / $3 (10)
| # | Option | Idea | Data |
|---|---|---|---|
| 181 | Daily stop −$15 | Stop for the day | ✅ |
| 182 | Daily stop −$10 | Tighter | ✅ |
| 183 | Daily profit lock | Stop after +$X to protect gains | ✅ |
| 184 | Hard floor $25 | Stop the bot if the balance falls to $25 | ✅ |
| 185 | Never-negative guarantee | Required (see money rules) | ✅ |
| 186 | Max trades per hour | Limit overtrading | ✅ |
| 187 | Cooldown after a loss | Skip the next round | ✅ |
| 188 | Drawdown-based pause | Pause if down X from the peak | ✅ |
| 189 | Weekly stop | Stop for the week at −$X | ✅ |
| 190 | Profit sweep | Treat profits above $50 as locked (never risked) | ✅ |

## N. Execution tactics (10)
| # | Option | Idea | Data |
|---|---|---|---|
| 191 | Immediate-or-cancel at +1¢ | Current live plan | ✅ |
| 192 | Immediate-or-cancel at exact price | Fewer fills, better prices | ✅ |
| 193 | Fill-or-kill | All or nothing | ✅ |
| 194 | Post-only limit (maker) | Earn instead of paying the spread | 🟡 |
| 195 | Maker-then-taker | Try a resting order, then cross | 🟡 |
| 196 | WebSocket price feed | Cut the 2 s polling delay | 🔴 (Polymarket API) |
| 197 | Pre-built orders | Prepare the order before the signal fires | ✅ |
| 198 | Retry-with-check | Never double-buy after an error | ✅ |
| 199 | Split order | Two $1.50 orders | ✅ |
| 200 | Slippage guard | Cancel if the fill price moves > 2¢ | ✅ |

## How to test 200 options without fooling ourselves

- **About 140 can be tested now (✅).** The others need recording changes (🟡), new feeds (🔴) or several days (📅).
- With 200 tries on the same 94 rounds, **roughly 10 will look excellent by pure luck**. So:
  1. **Batch:** test in groups of about 20 on the recorded data, walk-forward only.
  2. **False-discovery control:** keep only options that beat the baseline after Benjamini–Hochberg
     correction for the number tested.
  3. **Holdout:** every survivor must also win on the next Paper Report, which none of them have seen.
  4. **Live shadow:** survivors run as research accounts for about 200 trades before any real-money use.
- **Most valuable first** (different information, testable now): 46, 113, 131, 163, 148, 69, 2, 47,
  179, 157, 149, 5.
