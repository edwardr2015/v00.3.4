import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "research"))
import replay_sim as rs  # noqa: E402

TERMS = dict(theta=.11, tick=.01, quantum=.01)


def q(slug, t, gap, mid, bid, ask, up):
    return dict(slug=slug, t=t, elapsed=t % 1000, gap_p=gap, mid=mid, bid=bid, ask=ask,
                bid_q=1000, ask_q=1000, terms=TERMS, up=up)


def test_first_qualifying_quote_per_market_and_pnl():
    quotes = [q("a", 10, .8, .6, .59, .61, True),     # qualifies at 10 s
              q("a", 20, .9, .7, .69, .71, True),     # later quote ignored for market a
              q("b", 1010, .2, .4, .39, .41, True)]   # gap says DOWN, market resolves UP -> loss
    A, slugs, up = rs.arrays(quotes)
    P = rs.precompute(A)
    r = rs.run(A, P, len(slugs), up, ("gap", 0, .5, 0, .99, -1))
    assert r["trades"] == 2 and r["wins"] == 1
    qa, ca = rs.fill(quotes[0], "UP")
    qb, cb = rs.fill(quotes[2], "DOWN")
    assert np.isclose(r["pnl"], (qa - ca) + (0 - cb))
    # agreement requires gap and market on the same side: market b says DOWN too (mid .4)
    assert rs.run(A, P, len(slugs), up, ("agree", 0, .5, 0, .99, -1))["trades"] == 2
    # a start second after every quote leaves nothing
    assert rs.run(A, P, len(slugs), up, ("gap", 999, .5, 0, .99, -1)) is None


def test_thin_book_is_not_filled():
    thin = dict(q("a", 10, .8, .6, .59, .61, True), ask_q=1)
    assert rs.fill(thin, "UP") == (0.0, 0.0)


def test_wilson_and_sample_size():
    lo, hi = rs.wilson(9, 10)
    assert 0.59 < lo < 0.6 and hi > .98
    assert rs.trades_needed(.95, .90) < rs.trades_needed(.92, .90)


def test_safeguard_play_enters_once_and_takes_probability_exit():
    import safeguards_sim as sg
    # 50c entry on UP (model .75 shrunk with mid .5 -> .625, EV ~ 15%), later bid jumps to 95c.
    entry = dict(slug="a", t=1, elapsed=10, gap_p=.75, mid=.50, bid=.49, ask=.50, bid_q=1e4, ask_q=1e4,
                 terms=TERMS, up=True)
    later = dict(entry, t=2, elapsed=20, gap_p=.75, mid=.95, bid=.95, ask=.96)
    r = sg.play([entry, later], True, dict(sg.BASE))
    assert r["side"] == "UP" and r["exited"] and r["proceeds"] > r["cost"]
    held = sg.play([entry, later], True, dict(sg.BASE, exit="hold"))
    assert not held["exited"]
    assert sg.play([entry], True, dict(sg.BASE, band=(.60, .70))) is None
