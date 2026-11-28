# BTC Prophet: notes for the next session

## What this is
A **paper-trading** bot (no real money) for Polymarket US 5-minute "Bitcoin Up or Down" markets.
The app is FastAPI + SQLite and runs on the owner's Mac from `~/Downloads/ai-btc-prophet-000.300.021`.
The baseline is v0.3.23, at commit `a764353` on this branch.

- Repo `edwardr2015/v00.3.4`, branch `claude/btc-prophet-completion-9ym8j7`. App code is in `btc-prophet/`.
- Latest build: `release/ai-btc-prophet-0.3.23-dev8.zip`. It passes 200 tests and 35 dashboard button checks, and its SHA-256 is in `release/SHA256SUMS`.
- To build a ZIP, list `git ls-files` in `btc-prophet/` and regenerate `MANIFEST.json` so it covers every file except itself. Keep the top folder named `ai-btc-prophet-*`, and make sure `prophet/updates.py verify()` passes.

## Rules the owner set (do not break)
- Keep real-money execution disabled. Never fabricate fills.
- Preserve all data and balances. Never overwrite or merge the two databases: the live one is `data/timing/prophet.sqlite3`, 637 MB. The old 177 MB one is in `BTC-Prophet-Backup-Before-023`.
- Don't merge to main or production without the owner's approval. Never put API keys in chat or GitHub.
- Future real-money plan: start with $50 and $3 bets. The bot can never add money or go negative.
- Give the owner terminal steps one line at a time. They are not a programmer.

## What we built (dev1 to dev8)
- **Bug fixes.** The SKIP reason was being overwritten. Model training is no longer capped at 300; the training limits are now settings.
- **Exits.** Holding to the end is the default. Set `PROPHET_PRIMARY_EXIT=probability` for early exit, which the owner now starts with. Shadow tables compare the two exit styles.
- **Model selection.** The automatic switch is off by default (`PROPHET_PRIMARY_SELECTION`). The dashboard shows which model it *would* pick and why.
- **Scoreboard.** A "Reset counter" button zeroes the dashboard numbers but keeps the all-time totals and data.
- **Vegas panel.** It shows the win streak, a roulette wheel that spins while the model decides (green and red slices only), the last 25 rounds (Up/Down/Win/Loss), and a win pop-up with sound that plays once and closes itself.
- **Sound.** The app looks for `win-sound.mp3/.m4a/.aac/.wav` next to the database, and otherwise plays the built-in `prophet/static/sounds/jackpot.wav`.
- **Reports and tools.** The Excel report gained exit and model-selection sheets and drops empty sheets. New tools: `tools/continuity_audit.py`, `tools/import_learning.py` (copies the old learning into a copy, never overwrites) and `tools/reproduce_primary.py`.
- **Research.** Scripts and results are in `research/`. Write-ups are in `release/` (SIMULATION-RESULTS, STRESS-TEST, OPTIONS-200 and others).

## What we found
- The best tested rule enters only between minutes 3 and 4. It needs three signals to agree: the reference gap, the market price and gap+speed. The price must be at most 90¢, and it sells early if the signal reverses. At $3 realistic fills it made **+$30.62 over 94 markets**. The reversal exit held up under testing, but part of its gain relies on stale order books, so treat it with caution. The sample is still small.
- A 90% win rate is only reachable by trading very rarely, at expensive prices. That means small profit per win and big losses when wrong. Profit matters more than win rate.
- In live Report 12 the models were retraining hourly. The **early-exit shadow was +$29.52 vs hold at −$17.73**, so early exit was turned on.
- The `polymarket-us` SDK 2.4.1 has no deposit or withdraw calls and no idempotency key, so never retry an order POST.

## Open items
1. DONE in dev9: the built-in win sound is now the owner's Pixabay casino jackpot MP3, made louder.
2. Confirm the owner is running dev8 with `PROPHET_PRIMARY_EXIT=probability`.
3. Next research build: run the "minute 3–4 + all-3-agree + ≤90¢ + reversal exit" rule as a separate shadow paper account, and compare it on fresh reports.
4. Optional: add read-only and dry-run Polymarket connection modes with the money rules above. Run `continuity_audit` and `import_learning` on the old 177 MB database.
5. The owner will upload new Excel Quick and Paper reports. Analyse wins, losses and P&L since dev8.

## dev10 (latest)
- New `prophet/selective50.py`: a separate paper account that starts at $50 and makes $3 trades with the best research rule (minute 3–4, all 3 agree, ≤90¢, reversal exit). It never goes below $0 and never adds money. It shows on the dashboard ("Selective $50 account") and in the Excel sheet "Selective $50 Account". The main $1,000 account is untouched. Tests are in `tests/test_selective50.py`.
- `scripts/polymarket_readonly_check.py`: a read-only connection test. The cloud session's network blocks gateway.polymarket.us and api.polymarket.us.

## dev13 (latest; built from the owner's dev12 ZIP, so it includes dev11–12)
- Owner was running dev12 (has Test Accounts and Selective $1000 at $50); dev11–12 were never pushed, so `btc-prophet/` now holds dev13 source built on the uploaded dev12.
- `prophet/remote.py` + `/remote` phone page: status, Pause/Resume, Quick report, **Check for update** and **Install now**. Nothing checks or installs on its own (owner's choice).
- Update feed: `release/LATEST.json` on this branch (build number, ZIP name, SHA-256). To ship dev14: build the ZIP (no directory entries: `zip -r -X -D`; no `data/` folder), add it to `release/`, update `LATEST.json` and `SHA256SUMS`, push. The owner then presses Check and Install on the phone.
- Launcher: restarts itself after Install/Restart (restart-requested marker in `data/timing/updates/`), resumes the prior paper mode. Phone access over Wi-Fi only when `data/timing/phone-remote-on` exists; it then requires a password.
- Not yet done: dev13 is not installed on the Mac. One-time install via the dev12 dashboard's Software updates page (ZIP + SHA-256 above), then quit and reopen Start Prophet.
- Tests: 222 passed, 3 skipped. The launcher restart loop has not been tested on a Mac yet.

## dev14
- Simulation (research, not in app): 60 days of Binance 1-second BTC (17,276 rounds); walk-forward GBM with momentum/volume/hour/path features + UP/DOWN mirror augmentation only tied the plain gap formula (Brier 0.1536 vs 0.1534). No extra predictive signal in BTC itself.
- On 174 live trades the bot's forecast averaged 68% vs market 58% vs actual 56% wins: overconfident; bigger "edge" lost more. Binance opening price differed from the bot's by ~$77 and gaps barely correlated, so the feed vs settlement source is suspect.
- dev14 adds shadow accounts reality_check / reality_check_hold (blend = market + 0.3*(model-market), 3% edge, 30-70c) and gap_40_49, plus `prophet/feed_check.py` and the "Price Feed Check" report sheet. Main account unchanged. Tests: 228 passed.

## dev15
- Fix: remote.Updater.restart used asyncio.get_event_loop() from a sync endpoint (thread pool); on Python 3.14 that raises, so dev13/14 installs staged and selected the release but never restarted (bot left paused, restart marker left behind). Now a threading.Timer sends SIGTERM.

## dev16
- LIVE CONFIRMED (cloud test, 10 rounds): the bot's BRTI (Polymarket /v1/asset-prices/history 1S) matches the official 60s averages only when shifted ~+29 s (0/10 exact at shift 0; $5-9 error at 0, ~$1 at best shift). Its timestamp says ~2 s old, but the values are ~30 s behind settlement. Next step (dev17): live reference from Kraken/Coinbase + measured basis, once the Mac recorder has spot data (cloud blocks those hosts).
- book_timestamp = exchange last-trade time (transactTime), NOT quote freshness. Earlier claim "exits on 30 s old prices" was wrong. Exit guard uses book_checked (fetch time) > 5 s.
- New: prophet/fresh.py (recorder, lag check, settlement_model account with learned vol scale, limit_down_55 maker sim), prophet/hourly.py (1h fade test), main-model switch /api/remote/main-model/{settlement|standard} (off), /api/export/recorder.csv.gz, report sheets Feed Lag Check / Fresh Data Accounts / 1-Hour Fade Test, exit_comparison uses real SOLD P&L.
- Horizon research: 5m 1/32 rules profitable out-of-sample, 15m 9/43, 1h 11/41; only 1h "fade 20c+ move at minute 45" held (+17.7% -> +17.0%, n=142, ~15% chance luck). Tests: 241 passed.

## 2.0.0 (build 20) = dev17 + system audit
- prophet/feeds.py: feed referee (Coinbase/Kraken/Bitstamp/Gemini WebSockets, median of healthy feeds, basis from official values, move-based lag check). Live cloud test: new feed shift +2/-6 s vs old Polymarket BRTI +25 s on the same rounds.
- prophet/audit.py: /api/audit + "System Audit" sheet + phone card (safety, reconciliation of every paper account, feed health, live-vs-official, stuck positions, calibration, profit with 95% range, errors).
- Main switch: standard (default) / settlement / settlement_live. websockets==15.0.1 added to dependencies; launcher checks it.

## 2.0.1 (build 21)
- fresh.settlement_up(live=True): the already-locked part of the closing average (last minute) now comes from the live feed referee (consensus + basis), not the ~25-30 s late Polymarket BRTI; refuses to forecast if live seconds have gaps (<80% coverage).

## 2.0.2 (build 22)
- main_model=selective: main mirrors selective50 entries ($5, <=90c) and its reversal exit. Launcher runs caffeinate -ims -w $$.
- fresh_sim.py (5,102 markets, BRTI history shifted -29 s): fresh-feed model error 0.1804 vs stale 0.2017 vs market 0.1576 at ~90 s; trading its edge: fresh -7.5c/$1 (62% wins) vs stale -11.7c/$1 (46%). Fresh data halves the gap but the market still forecasts better at minute resolution.

## 2.1.0 (build 23)
- prophet/live.py LiveTrader (owner asked to go live: $50, $3/trade). Follows selective50 paper decisions; IOC limit orders, sent once, never retried; error -> OFF. Reversal exit (SELL_LONG/SELL_SHORT IOC at bid-1 tick). Modes in data/timing/live-mode, local-only endpoint, LIVE needs X-Prophet-Confirm: real-money and a confirmed short-price convention (data/timing/live-convention.json from scripts/live_test.py --buy).
- NOT YET VERIFIED against the real exchange: how BUY_SHORT price is read (DOWN price vs UP price). live_test.py previews both; for marketable IOC orders both fill at market, only the protection limit differs.
- Audit: connection checks per feed, real-money check.

## 2.1.1 (build 24)
- prophet/watchdog.py: remembers owner Start/Resume vs Pause/Stop/Emergency (store owner_mode); every 30 s resumes SIMULATE when reference+book are fresh. Owner reported having to press Resume after disconnects; live trader depends on selective50 paper signals, which need SIMULATE.

## 2.2.0 (build 25)
- prophet/brain.py: brain_thoughts (one row per round-minute: market/main/settlement/settlement_live), per-minute Brier + learned trust (market + w*(model-market)), account leaderboard with 95% range, plain-English summary; /api/brain, phone card, "Brain" sheet. Recommends only.
- Neural stays off: torch>=2.5 has no macOS x86_64 wheels (owner iMac is Intel).

## 2.3.0 (build 26)
- paper.py loop: smart.tick() (research: lab, model_paper, exits, observer) isolated; its exceptions log a WARNING instead of halt(CRITICAL_SYSTEM_ERROR). Likely cause of owner's manual Resume after "disconnects".
- Lean default (PROPHET_LEAN=1, PROPHET_RESEARCH_MODELS=none unless data/timing/full-research exists): paper.smart=None, corrective research off, no research libs install.
- collector: book no longer archived (observations source=book was never read; likely most of the 637 MB).
- scripts/compact.py: needs prophet.lock free, backs up (needs 1.3x free disk), deletes observations source=book, recorder>21d, candles>30d, feed_ticks>14d, VACUUM + wal_checkpoint.

## 2.4.0 (build 27)
- write_quick_workbook = decision report (Start Here, Real Money Trades, DECISION_SHEETS via _Only filter). Old quick = write_research_workbook, /api/export/research.xlsx. add_live also in full workbook.
- Report sections wrapped in _section/_safe_init: read-only DB from older version no longer crashes (found on live17 DB: brain.initialize on read-only).

## 2.5.0 (build 28)
- static/winsound.js: Web Audio unlock on first gesture/"Enable sound", decoded once, plays on win (autoplay policy was blocking <audio>.play from timers). Dashboard + /remote (main, selective50, live wins). Browser-tested: scripts/browser_check_sound.js (node playwright).
- research-only panels hidden when /api/state lean=true; asset ?v=2.5.0 cache bust.
