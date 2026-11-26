"""Explicit offline model training; does not change the primary strategy."""

import argparse
from prophet.history import History
from prophet.models import Models
from prophet.research import examples
from prophet.store import Store

if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--history", default="data/history.sqlite3")
    p.add_argument("--database", default="data/prophet.sqlite3")
    p.add_argument("--models", nargs="+", default=["logistic", "ensemble"])
    args = p.parse_args()
    history = History(args.history, "data/history-cache")
    try:
        a, b = history.db.execute("SELECT min(start),max(end) FROM markets").fetchone()
        if a is None:
            raise SystemExit("No historical labelled data available")
        rows, rejected = examples(history, a, b, entry_offset=60)
    finally:
        history.close()
    registry = Models(Store(args.database))
    for family in args.models:
        try:
            version, metrics = registry.train(family, rows)
            print(
                version, "test:", metrics["test"], "historical exploratory; profitability UNKNOWN"
            )
        except (ValueError, ImportError) as e:
            print(family, "BLOCKED:", e)
