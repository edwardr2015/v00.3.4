"""Score stored research configurations in chronological, read-only batches."""
import argparse
import json
from pathlib import Path
import sqlite3


def run(database, limit=50):
    if not 1<=limit<=1000:
        raise ValueError("Choose 1–1000 configurations per batch.")
    path=Path(database).resolve()
    with sqlite3.connect(path.as_uri()+"?mode=ro",uri=True) as db:
        db.row_factory=sqlite3.Row
        configs=db.execute("""SELECT * FROM lab_configs WHERE id LIKE 'timing/v2/%'
             ORDER BY checkpoint,family,stake,version LIMIT ?""",(limit,)).fetchall()
        report=[]
        for c in configs:
            settled=list(db.execute(
                """SELECT o.time,o.pnl,o.slug FROM lab_orders o JOIN verified_markets v ON v.slug=o.slug
                   WHERE o.config=? AND o.pnl IS NOT NULL AND o.time<v.end
                   ORDER BY o.time,o.id""",(c["id"],)))
            split=(len(settled)*4)//5
            validation=settled[:split]
            future=settled[split:]
            report.append(dict(config=c["id"],version=c["version"],status=c["status"],
                observed=len(settled),skipped=db.execute("SELECT count(*) FROM lab_skips WHERE config=?",(c["id"],)).fetchone()[0],
                early_pnl=sum(r["pnl"] for r in validation) if validation else None,
                late_pnl=sum(r["pnl"] for r in future) if future else None,
                late_trades=len(future),later_start=future[0]["time"] if future else None,
                later_wins=sum(r["pnl"]>0 for r in future)))
        return dict(scope="Existing settled forward paper ledgers. Chronological descriptive split. Configurations share rounds.",
                    configurations=report)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database")
    parser.add_argument("--limit",type=int,default=50)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    args.output.write_text(json.dumps(run(args.database,args.limit),indent=2,allow_nan=False)+"\n")

if __name__=="__main__":
    main()
