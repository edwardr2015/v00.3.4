# Polymarket BTC 5-minute markets: large-sample research (2026-11-27)

Data: 5,141 official Polymarket US 5m markets (Nov 1-18, 2026; per-minute UP/DOWN prices from
/v1/price-history, official settlement from /v1/markets/{slug}/settlement), BRTI 1-second index
(/v1/asset-prices/history), 60 days of Binance 1s BTCUSDT (17,276 synthetic rounds), 174 live bot trades.

1. Settlement: official rule = simple average of 60 BRTI prices in the minute before start/end.
   The BRTI history endpoint reproduces 99.3% of results only with a ~+15-30 s label shift, i.e. its
   1 s values appear ~30 s behind real time. The bot uses this same endpoint as its live reference.
2. A 30-s-old price makes the gap forecast 8-21% worse (Brier) than a fresh one (Binance 60 days).
3. Market is well calibrated; ~$95k notional per round, deep professional liquidity.
4. 117 market-only rules (price band, momentum, fade, streak, hour): picked on first 60% of days,
   only 11/117 profitable on the last 40% after fees (31/117 with zero costs) - consistent with no edge.
5. One consistent bias: UP priced 40-50c wins ~39-40% (n=708 at ~59 s, n=240 at ~179 s), i.e. DOWN
   near 55c is slightly underpriced. After taker fees (0.11*p*(1-p)) + 1c slippage ~ breakeven;
   only meaningful if bought without paying spread/fees (limit orders). Period had UP 49%: may be regime.
6. ML features (momentum, volume, hour, path, mirror augmentation) do not beat the plain gap formula.
