# Online research → tested features (2026-11-26)

Each idea from the literature was tested on the 94 recorded markets (Paper Report, 8,985 fresh
quotes, app fill model) before anything was added. Scripts: `research/feature_research.py`;
output: `research/feature-research-results.json`.

## What the research says

| Topic | Finding | Source quality |
|---|---|---|
| Polymarket 5-min BTC bots | The main edge discussed is **latency arbitrage**: Binance moves first, Polymarket's book reprices later. Lag claims range from ~100 ms to 30–90 s. Write-ups say the model side has no edge and that latency pays. Polymarket added taker fees and dynamic fees to curb it. | GitHub/blog write-ups, anecdotal |
| Order-flow imbalance (Cont, Kukanov, Stoikov 2014) | Short-horizon price changes are driven mainly by the imbalance of best bid/ask size changes, linear with slope ∝ 1/depth | Peer-reviewed (J. Financial Econometrics) |
| Microprice (Stoikov) | A size-weighted mid predicts short-term moves better than the mid | Peer-reviewed / slides |
| Favourite–longshot bias on Polymarket | A large study (188,509 markets) finds systematic under-confidence, recalibration slope ≈ 1.11, stronger at long horizons | Thesis / preprints |
| BTC price discovery | Most BTC price moves start on Binance (~70%), then Coinbase; Coinbase led Kraken by ~1.4 s in one event study | Mixed: academic + industry |
| BRTI | Consolidated order books of constituent exchanges; stale venues (>10 s) excluded; size caps | CF Benchmarks methodology |
| Settlement | Close = mean of 60 one-second BRTI values before the boundary; ties settle UP | Polymarket US FAQ (cited in app docs) |
| Volatility | HAR-type realised-volatility models beat GARCH at short horizons; no study at seconds horizons | Peer-reviewed, daily horizon |

## What held up on your data (lower Brier is better)

| Seconds in | Market mid | Microprice | Gap (current) | Settlement-average | Market recalibrated |
|---|---|---|---|---|---|
| 0–60 | **0.242** | 0.243 | 0.256 | 0.257 | 0.283 |
| 60–120 | **0.193** | 0.192 | 0.226 | 0.230 | 0.207 |
| 120–180 | 0.162 | **0.161** | 0.173 | 0.177 | 0.175 |
| 180–240 | **0.114** | 0.115 | 0.120 | 0.122 | 0.122 |
| 240–300 | 0.066 | 0.066 | 0.077 | 0.082 | **0.062** |

* **Microprice** = market mid here (1¢ spreads); it adds nothing on this venue.
* **Favourite–longshot recalibration** only helps in the final minute.
* **The gap model is overconfident:** scaling its volatility by 1.5× improves Brier from 0.178 to
  0.173, chosen walk-forward, but it still trails the market (0.163).
* **The settlement-average correction alone doesn't help**, because it makes an already
  overconfident model more confident. It is kept with the 1.5× volatility scale.
* Nothing built on BRTI alone beats the market price. That is consistent with the latency
  research: market makers see faster feeds.

## What made money in both halves (pre-declared rules, $5, hold to settlement)

| Rule | Trades | Win rate | P&L | 1st half | 2nd half |
|---|---|---|---|---|---|
| Gap model, after 180 s, probability ≥ price + 10¢ | 39 | 80% | +$33.84 | +$20.70 | +$13.14 |
| Settlement-average, after 180 s, + 10¢ | 46 | 80% | +$33.68 | +$22.13 | +$11.55 |
| ½ settlement + ½ market, after 90 s, + 5¢ | 78 | 72% | +$22.86 | +$18.43 | +$4.43 |
| Same gap rule from 0 s | 90 | 59% | −$26.84 | −$10.33 | −$16.51 |

Walk-forward (rule chosen on markets 1–47, frozen, run on 48–94): gap → 16/23, +$8.14;
settlement → 25/34, +$6.04; blend → 19/26, +$11.55. All three chose "start at 180 s".

**Pattern: disagreeing with the market late in the round (last two minutes) has paid; early it
has lost.** This fits stale-book/latency behaviour: late in the round the BRTI is close to
the averaged settlement value while the book can lag.

## Added to the app (research shadows only, no primary ledger writes)

`prophet/settlement_model.py` and `prophet/corrective.py`, frozen as pre-declared rules:

| Policy | Rule |
|---|---|
| `late_gap_180s_abs10` | Reference Gap, elapsed ≥ 180 s, probability − all-in price ≥ 10¢ |
| `settle_avg_180s_abs10` | Settlement-average model (vol × 1.5), elapsed ≥ 180 s, ≥ 10¢ |
| `settle_mid_blend_90s_abs05` | ½ settlement + ½ midpoint, elapsed ≥ 90 s, ≥ 5¢ |
| `primary_rule_edge0_40_70` | The primary rule with the minimum edge lowered from 10% to 0% (safeguard variant C) |
| `settlement_average` (challenger) | Forecast recorded at 90 s, scored on Brier/log loss |

Safeguard variant J (model + market agreement) already exists as `gap_agreement_edge10`.
Each records standard- and higher-cost fills on real captured quotes and appears in the
*Forward Research* sheet. First checkpoint: 200 settled forward trials each.

## Not added, needs a decision

**A Binance (or Coinbase) trade feed for lead-lag.** This is the most-cited edge, but it needs a
new collector, a venue that serves your region (Binance.com blocks US users), and measurement
of the app's own latency. Polymarket's dynamic fees were introduced to remove exactly this
edge, so it may already be gone.

## Sources
- [weck02/polybtcbot – latency-arbitrage bot for Polymarket 5-min BTC](https://github.com/weck02/polybtcbot)
- [TimonGisler/polymarket-latency-arb](https://github.com/TimonGisler/polymarket-latency-arb)
- [AmarRSehgal/polymarket-btc-options – Black-Scholes N(d2) with EWMA vol](https://github.com/AmarRSehgal/polymarket-btc-options)
- [Stale order book sniping on Polymarket (DEV)](https://dev.to/abrownfox001/how-real-5-minute-btc-scalpers-work-on-polymarket-stale-order-book-sniping-4727)
- [Bots now run Polymarket's 5-minute BTC markets (asksurf)](https://asksurf.ai/pulse/en/latency-arbitrage-dominates-polymarket-btc)
- [Polymarket dynamic fees to curb latency arbitrage (Finance Magnates)](https://www.financemagnates.com/cryptocurrency/polymarket-introduces-dynamic-fees-to-curb-latency-arbitrage-in-short-term-crypto-markets/)
- [Polymarket taker fees on 15-minute crypto markets (The Block)](https://www.theblock.co/post/384461/polymarket-adds-taker-fees-to-15-minute-crypto-markets-to-fund-liquidity-rebates)
- [Cont, Kukanov, Stoikov – The price impact of order book events](https://arxiv.org/abs/1011.6402)
- [Stoikov – microprice slides](https://www.ma.imperial.ac.uk/~ajacquie/Gatheral60/Slides/Gatheral60%20-%20Stoikov.pdf)
- [Favourite–longshot bias in Polymarket](https://arxiv.org/pdf/2609.12878)
- [Princeton thesis on Polymarket calibration](https://theses-dissertations.princeton.edu/entities/publication/d5708372-5ea3-4252-b669-7f9cee07387c)
- [Almost all bitcoin price transmission comes from Binance](https://www.socialscience.international/almost-all-bitcoin-price-transmission-comes-from-binance)
- [Makarov & Schoar – Price discovery in cryptocurrency markets](https://personal.lse.ac.uk/makarov1/index_files/PriceDiscoveryCrypto.pdf)
- [CME CF Real Time Indices methodology](https://docs.cfbenchmarks.com/CME%20CF%20Real%20Time%20Indices%20Methodology.pdf)
- [Forecasting the volatility of Bitcoin (HAR models)](https://centaur.reading.ac.uk/87932/1/R%26R_Forecasting%20the%20Volatility%20of%20Bitcoin_complete.pdf)
