"""Import explicitly US PMData L2 files and select causally available snapshots.

No credentials or paid downloads occur here. Optional reader: pip install '.[depth]'.
"""

import argparse
import hashlib
import json
import math
from pathlib import Path


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS historical_depth (
      slug TEXT,source_ns INTEGER,received_ns INTEGER,book TEXT,file_sha256 TEXT,
      PRIMARY KEY(slug,source_ns,received_ns))""")
    db.commit()


def validate_row(row, slug):
    if row["market_slug"] != slug or row["event_type"] != "marketData":
        raise ValueError("Wrong market or event type in US depth file")
    source, received = int(row["timestamp"]), int(row["local_timestamp"])
    if source < 10**17 or received < source - 5 * 10**9:
        raise ValueError("Invalid nanosecond timestamps")
    sides = {}
    for pp, qq, name, descending in [
        ("bid_prices", "bid_sizes", "bids", True),
        ("offer_prices", "offer_sizes", "asks", False),
    ]:
        prices, sizes = row[pp], row[qq]
        if len(prices) != len(sizes):
            raise ValueError("Price/quantity arrays have different lengths")
        if any(not math.isfinite(p) or not 0 <= p <= 1 for p in prices):
            raise ValueError("Invalid book price")
        if any(not math.isfinite(q) or q < 0 for q in sizes):
            raise ValueError("Invalid book quantity")
        if prices != sorted(prices, reverse=descending):
            raise ValueError("Book levels are not best-first")
        sides[name] = [dict(price=p, quantity=q) for p, q in zip(prices, sizes) if q > 0]
    if sides["bids"] and sides["asks"] and sides["bids"][0]["price"] >= sides["asks"][0]["price"]:
        raise ValueError("Crossed or locked historical book")
    return dict(
        slug=slug,
        state=row["state"],
        timestamp=source / 10**9,
        source_ns=source,
        received_ns=received,
        **sides,
    )


def import_file(db, path, slug, entry_only=True):
    import pyarrow as pa
    import pyarrow.parquet as pq

    initialize(db)
    known = db.execute("SELECT payload FROM markets WHERE slug=?", (slug,)).fetchone()
    if not known:
        raise ValueError(
            "First verify this exact US BTC contract through the official market importer"
        )
    m = json.loads(known[0])
    terms = m.get("assetPriceTerms") or {}
    if terms.get("indexSymbol") != "BRTI" or (terms.get("asset") or {}).get("symbol") != "btc":
        raise ValueError("Not a verified US BTC BRTI market")
    path = Path(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    last = None
    count = 0
    rejected = 0
    from .providers import timestamp

    decision_ns = (int(timestamp(terms["windowStart"])) + 60) * 10**9
    chosen = None
    with db:
        for batch in pq.ParquetFile(path).iter_batches(batch_size=1024):
            for name in ("timestamp", "local_timestamp"):
                index = batch.schema.get_field_index(name)
                if (
                    index < 0
                    or not pa.types.is_timestamp(batch.schema.field(name).type)
                    or batch.schema.field(name).type.unit != "ns"
                ):
                    raise ValueError("Depth timestamps must retain nanosecond precision")
                batch = batch.set_column(index, name, batch.column(index).cast(pa.int64()))
            for row in batch.to_pylist():
                if row.get("market_slug") != slug or row.get("event_type") != "marketData":
                    raise ValueError("Wrong market or event type in US depth file")
                order = (row["timestamp"], row["local_timestamp"])
                if last and order < last:
                    raise ValueError("File order changed; do not re-sort the vendor file")
                last = order
                try:
                    book = validate_row(row, slug)
                except ValueError:
                    rejected += 1
                    if (
                        entry_only
                        and row["timestamp"] <= decision_ns
                        and row["local_timestamp"] <= decision_ns
                    ):
                        chosen = dict(
                            slug=slug,
                            state="INVALID",
                            timestamp=row["timestamp"] / 1e9,
                            source_ns=row["timestamp"],
                            received_ns=row["local_timestamp"],
                            bids=[],
                            asks=[],
                        )
                    continue
                if entry_only:
                    if book["source_ns"] <= decision_ns and book["received_ns"] <= decision_ns:
                        chosen = book
                    count += 1
                    continue
                db.execute(
                    "INSERT OR REPLACE INTO historical_depth VALUES (?,?,?,?,?)",
                    (slug, book["source_ns"], book["received_ns"], json.dumps(book), digest),
                )
                count += 1
        if entry_only and chosen:
            db.execute(
                "INSERT OR REPLACE INTO historical_depth VALUES (?,?,?,?,?)",
                (slug, chosen["source_ns"], chosen["received_ns"], json.dumps(chosen), digest),
            )
    return dict(
        slug=slug,
        accepted=count,
        rejected=rejected,
        sha256=digest,
        entry_only=entry_only,
        entry_snapshot_saved=bool(chosen) if entry_only else None,
    )


def at(db, slug, decision_ns, max_age_seconds=15):
    exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='historical_depth'"
    ).fetchone()
    if not exists:
        return None
    row = db.execute(
        """SELECT book,file_sha256 FROM historical_depth WHERE slug=? AND source_ns<=? AND received_ns<=?
      ORDER BY source_ns DESC,received_ns DESC LIMIT 1""",
        (slug, decision_ns, decision_ns),
    ).fetchone()
    if not row:
        return None
    book = json.loads(row[0])
    book["file_sha256"] = row[1]
    if (
        book["state"] != "MARKET_STATE_OPEN"
        or decision_ns - book["source_ns"] > max_age_seconds * 10**9
    ):
        return None
    return book


if __name__ == "__main__":
    import sqlite3

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("file")
    p.add_argument("--slug", required=True)
    p.add_argument("--db", default="data/history.sqlite3")
    args = p.parse_args()
    with sqlite3.connect(args.db) as db:
        print(json.dumps(import_file(db, args.file, args.slug)))
