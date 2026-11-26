"""Non-model financial limits for the v0.2 experiment."""

from datetime import datetime
from zoneinfo import ZoneInfo
import math

CHICAGO = ZoneInfo("America/Chicago")
CHECKPOINTS = (15, 30, 45, 60, 75, 90)


def local_day(now):
    return datetime.fromtimestamp(now, CHICAGO).date().isoformat()


def daily_loss(orders, now):
    """Gross realized losses plus cost at risk, never offset by wins."""
    return sum(
        o["cost"]
        if o["status"] == "OPEN"
        else max(0, -o["pnl"])
        if o["pnl"] is not None and local_day(o["settled"]) == local_day(now)
        else 0
        for o in orders
    )


def entry_block(account, orders, stake, now, *, daily_cap=True):
    if account["halted"]:
        return "MANUAL_REVIEW_REQUIRED"
    if not math.isfinite(stake) or not 0 < stake <= 3:
        return "MAXIMUM_STAKE_3"
    if any(o["status"] == "OPEN" for o in orders):
        return "ONE_POSITION_LIMIT"
    if account["balance"] < stake:
        return "INSUFFICIENT_CASH"
    if daily_cap and daily_loss(orders, now) + stake > 20 + 1e-9:
        return "DAILY_LOSS_RESERVE"
    return None
