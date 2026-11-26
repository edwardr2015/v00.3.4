"""Load timestamp-valid executable quotes + official outcomes from a BTC Prophet Full (Paper) Report."""
import json
import math
import pickle
from pathlib import Path

import openpyxl


def load(workbook, cache=None):
    if cache and Path(cache).exists():
        return pickle.load(open(cache, "rb"))
    wb = openpyxl.load_workbook(workbook, read_only=True, data_only=True)
    outcome = {r[0]: r[3] for r in list(wb["UP DOWN History"].iter_rows(values_only=True))[1:]
               if r[3] in ("UP", "DOWN")}
    quotes = []
    for r in list(wb["Forward Quote Audit"].iter_rows(values_only=True))[1:]:
        e = json.loads(r[2])
        if e.get("reason") or r[0] not in outcome:      # only fresh, usable, settled quotes
            continue
        if not (e.get("top_bid") and e.get("top_ask") and e.get("opening") and e.get("reference_price")):
            continue
        quotes.append(dict(
            slug=r[0], t=e["decision"], left=e["seconds_left"], elapsed=300 - e["seconds_left"],
            ref=e["reference_price"], opening=e["opening"], vol=e["volatility"],
            mid=e["midpoint"], gap_p=e["gap_probability"], spread=e["spread"],
            bid=e["top_bid"]["price"], bid_q=e["top_bid"]["quantity"],
            ask=e["top_ask"]["price"], ask_q=e["top_ask"]["quantity"], terms=e["terms"],
            gap_bps=math.log(e["reference_price"] / e["opening"]) * 1e4,
            up=outcome[r[0]] == "UP"))
    quotes.sort(key=lambda q: q["t"])
    data = dict(outcome=outcome, quotes=quotes)
    if cache:
        pickle.dump(data, open(cache, "wb"))
    return data
