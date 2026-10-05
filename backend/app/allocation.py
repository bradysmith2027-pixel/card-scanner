"""
allocation.py

Splits one amount of money across a bunch of cards so it adds back up exactly.

I need this in two places:
    Trades: the cost of what I gave up (plus any cash) moves onto the cards I got
    Lots:   the price of the whole lot gets spread across every card in it

Two rules:

    1. Split by value, not evenly. If a $120 card and a $0.50 common each got
       half the cost, the big card would look like an amazing flip and the
       commons would look like huge losses. The ROI numbers would be useless.

    2. The pieces have to add up to the total, to the cent. Normal rounding
       splits $100 three ways into $33.33 x3 = $99.99 and loses a penny.

Trades and lots both use this so there's only one copy of the logic.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Sequence

CENTS = Decimal("0.01")


def largest_remainder(
    total: Decimal, weights: Sequence[Decimal], refs: Sequence[str]
) -> dict[str, Decimal]:
    """Split total by weights so the pieces add up to exactly total.

    Rounds every share down to the cent, then hands out the leftover pennies
    one at a time to whoever got rounded down the most. Ties go in the original
    order, so it gives the same answer every time.

    weights has to add up to more than 0. If there are no values to go on, the
    caller just passes equal weights.
    """
    weight_total = sum(weights, Decimal(0))
    exact = [total * w / weight_total for w in weights]
    floored = [e.quantize(CENTS, rounding="ROUND_DOWN") for e in exact]

    leftover = int(((total - sum(floored, Decimal(0))) / CENTS).to_integral_value())

    order = sorted(range(len(exact)), key=lambda i: (-(exact[i] - floored[i]), i))
    for k in range(leftover):
        floored[order[k % len(order)]] += CENTS

    return dict(zip(refs, floored))
