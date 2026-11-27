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
