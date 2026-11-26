# Import your old learning history (the ~177 MB database)

`tools/import_learning.py` copies **only learning data** (training labels, plus optional history
markets and price bars) from the old database into a **new copy** of your current database.
Balances, trades, settlements and accounts are never copied or changed. Your existing files are
opened read-only.

## 1. Get the tool

Download from GitHub (`edwardr2015/v00.3.4`, branch `claude/btc-prophet-completion-9ym8j7`) the
folder `tools/`, or the two files `tools/import_learning.py` and `tools/continuity_audit.py`.
Put the `tools/` folder inside your project folder, next to `data/`.

## 2. Check what you have first (read-only)

In Terminal, from your project folder:

```bash
python3 tools/continuity_audit.py \
  --store old=data/prophet.sqlite3 --store current=data/timing/prophet.sqlite3 \
  --history old=data/history.sqlite3 --history current=data/timing/history.sqlite3 \
  --json continuity-audit.json
```

Leave out any line whose file doesn't exist. This shows how many usable training markets each
database holds and how many are unique across them.

## 3. Build the combined copy (app can keep running)

```bash
python3 tools/import_learning.py \
  --source data/prophet.sqlite3 --target data/timing/prophet.sqlite3 \
  --out data/timing/prophet-imported.sqlite3 \
  --history-source data/history.sqlite3 --history-target data/timing/history.sqlite3 \
  --history-out data/timing/history-imported.sqlite3 \
  --report import-report.json
```

If there is no `data/history.sqlite3`, drop the three `--history-…` lines. Check the report
shows `"integrity": "ok"`, how many labels were imported, and any conflicts (markets whose
outcome differs between databases; they are skipped).

## 4. Switch to the combined copy

1. Stop the app (Control+C).
2. Keep the current files:
   ```bash
   mv data/timing/prophet.sqlite3 data/timing/prophet-before-import.sqlite3
   mv data/timing/history.sqlite3 data/timing/history-before-import.sqlite3
   rm -f data/timing/prophet.sqlite3-wal data/timing/prophet.sqlite3-shm \
         data/timing/history.sqlite3-wal data/timing/history.sqlite3-shm
   ```
   (Delete the `-wal`/`-shm` side files only after the app has stopped. SQLite folds them into the main
   file on a clean shutdown, and the imported copy already contains everything.)
3. Put the combined copy in place:
   ```bash
   mv data/timing/prophet-imported.sqlite3 data/timing/prophet.sqlite3
   mv data/timing/history-imported.sqlite3 data/timing/history.sqlite3
   ```
4. Start the app the usual way: `bash "Start Prophet.command"`.

Within about 5 minutes the learning status should show many more examples, and the models train
on old and new markets together.

## Rollback

Stop the app, then:
```bash
mv data/timing/prophet-before-import.sqlite3 data/timing/prophet.sqlite3
mv data/timing/history-before-import.sqlite3 data/timing/history.sqlite3
```

## Important

* **Always start the app with `Start Prophet.command`.** Started any other way (e.g. `uvicorn`
  directly), it defaults to `data/prophet.sqlite3`, which is your old database, and would
  upgrade it in place.
* Don't delete the old database until the import is confirmed. Keep it as the original record.
