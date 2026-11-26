"""Hard risk limits. Prediction probabilities cannot change these limits."""

from datetime import datetime, timezone

CONSECUTIVE_LIMIT = 5
DAILY_LIMIT = 10.0
TOTAL_LIMIT = 20.0


def utc_day(now):
    return datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")


def entry_block(account, exposure, requested, now):
    if account["halted"]:
        return "MANUAL_REVIEW_REQUIRED"
    if account["streak"] >= CONSECUTIVE_LIMIT:
        return "FIVE_CONSECUTIVE_LOSSES"
    day_pnl = account["day_pnl"] if account["day"] == utc_day(now) else 0
    if day_pnl <= -DAILY_LIMIT + 1e-9:
        return "DAILY_LOSS_LIMIT"
    if account["realized"] <= -TOTAL_LIMIT + 1e-9:
        return "TOTAL_LOSS_LIMIT"
    if exposure > 0:
        return "ONE_POSITION_LIMIT"
    # Reserve the worst possible loss of the proposed order, including its fees.
    if day_pnl - requested < -DAILY_LIMIT - 1e-9:
        return "DAILY_LOSS_RESERVE"
    if account["realized"] - requested < -TOTAL_LIMIT - 1e-9:
        return "TOTAL_LOSS_RESERVE"
    if account["balance"] < requested:
        return "INSUFFICIENT_BALANCE"
    return None


def settlement_block(account):
    if account["streak"] >= CONSECUTIVE_LIMIT:
        return "FIVE_CONSECUTIVE_LOSSES"
    if account["day_pnl"] <= -DAILY_LIMIT + 1e-9:
        return "DAILY_LOSS_LIMIT"
    if account["realized"] <= -TOTAL_LIMIT + 1e-9:
        return "TOTAL_LOSS_LIMIT"
    return None
