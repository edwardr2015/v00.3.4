"""A same-round, same-checkpoint vote across the eight source model families."""
import math


def vote(components, required):
    up = sum(item["up"] > .5 for item in components.values())
    down = sum(item["up"] < .5 for item in components.values())
    ties = len(components)-up-down
    missing = sorted(set(required)-components.keys())
    share = (up+.5*ties)/len(required) if not missing else None
    mean_up = math.fsum(item["up"] for item in components.values())/len(required) if not missing else None
    return dict(up_votes=up,down_votes=down,ties=ties,missing=missing,up_share=share,
                mean_up=mean_up,components=components)


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS model_vote_forecasts (
        slug TEXT, checkpoint INTEGER, time REAL NOT NULL, evidence TEXT NOT NULL,
        PRIMARY KEY(slug,checkpoint)
    )""")
