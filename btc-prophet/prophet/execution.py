"""Deterministic paper fills against observed US YES book depth, never real orders."""

from decimal import Decimal, ROUND_DOWN, ROUND_HALF_EVEN


def fee(exact):
    return float(Decimal(str(exact)).quantize(Decimal(".01"), rounding=ROUND_HALF_EVEN))


def levels(book, side, buying=True):
    if side == "UP":
        rows = book["asks" if buying else "bids"]
    else:
        rows = [
            dict(price=1 - r["price"], quantity=r["quantity"])
            for r in book["bids" if buying else "asks"]
        ]
    return sorted(rows, key=lambda r: r["price"], reverse=not buying)


def execute(
    book,
    side,
    *,
    budget=None,
    quantity=None,
    buying=True,
    theta=0.095,
    tick=0.01,
    quantum=0.01,
    participation=0.25,
    slippage_ticks=1,
):
    """IOC assumption: 25% of displayed depth, one adverse tick, no resting fills.

    A buy budget includes fees. Fractional quantities use the market's minimum
    quantity as a conservative sizing grid. Per-fill fee adjustment follows the
    documented cumulative banker's-rounded cap. No rebate is credited.
    """
    if side not in ("UP", "DOWN") or not 0 < tick < 1 or not 0 < quantum <= 100:
        raise ValueError("Invalid execution terms")
    if not 0.0695 <= theta <= 1 or not 0 < participation <= 1 or slippage_ticks < 0:
        raise ValueError("Invalid cost assumptions")
    remaining = float(budget) if buying else float(quantity)
    if remaining < 0:
        raise ValueError("Negative order")
    qty = gross = commission = exact_fee = 0.0
    fills = []
    for row in levels(book, side, buying):
        p = round(row["price"] + (1 if buying else -1) * slippage_ticks * tick, 10)
        if not 0 < p < 1:
            continue
        available = row["quantity"] * participation
        affordable = remaining / (p * (1 + theta * (1 - p))) if buying else remaining
        q = float(
            (Decimal(str(min(available, affordable))) / Decimal(str(quantum))).to_integral_value(
                rounding=ROUND_DOWN
            )
            * Decimal(str(quantum))
        )
        if q < quantum:
            continue
        while q >= quantum:
            exact = theta * q * p * (1 - p)
            f = max(0, min(fee(exact), fee(exact_fee + exact) - commission))
            cost = q * p + f
            if not buying or cost <= remaining + 1e-10:
                break
            q = round(q - quantum, 10)
        if q < quantum:
            continue
        exact_fee += exact
        commission = round(commission + f, 10)
        gross += q * p
        qty += q
        remaining -= cost if buying else q
        fills.append(dict(quantity=q, price=p, fee=f))
        if remaining < (quantum * 0.001 if buying else quantum):
            break
    total = gross + commission if buying else gross - commission
    return dict(
        quantity=round(qty, 10),
        gross=round(gross, 10),
        fee=commission,
        total=round(total, 10),
        average_price=gross / qty if qty else None,
        unfilled=max(0, remaining),
        status="UNFILLED"
        if not qty
        else "PARTIAL"
        if remaining > (0.02 if buying else quantum / 2)
        else "FILLED",
        fills=fills,
        assumptions=dict(
            theta=theta,
            participation=participation,
            slippage_ticks=slippage_ticks,
            tick=tick,
            quantum=quantum,
        ),
    )


def paper_buy(book, side, budget, **terms):
    """Reject one-cent quotes and fills deploying less than 95% of the budget."""
    quotes = levels(book, side)
    if not quotes:
        return None, "NO_EXECUTABLE_DEPTH"
    if quotes[0]["price"] <= .01 + 1e-10:
        return None, "ONE_CENT_QUOTE"
    fill = execute(book, side, budget=budget, **terms)
    if not fill["quantity"]:
        return None, "NO_EXECUTABLE_DEPTH"
    if fill["total"] < .95 * budget:
        return None, "UNDERSIZED_FILL"
    return fill, None


def compare_stakes(book, side, probability, terms, minimum_edge, stakes):
    """Price independent shadow trials against the same book."""
    results = []
    for stake in stakes:
        fill, reason = paper_buy(book,side,stake,**terms)
        edge = (probability*fill["quantity"]-fill["total"])/fill["total"] if fill else None
        if edge is not None and edge < minimum_edge:
            reason = "NO_POSITIVE_EDGE"
        results.append(dict(stake=stake,side=side,mean_probability=probability,edge_rate=edge,
                            reason=reason or "SHADOW_FILL",modeled_fill=fill))
    return results
