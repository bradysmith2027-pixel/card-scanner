"""
allocation.py — split one amount of money across many items, exactly.

WHY THIS EXISTS (2026-09-14)
    Dreamboat has two places where a single payment must become many per-card
    cost bases:

        TRADES — basis given up (+ cash boot) carries onto the cards received
        LOTS   — one lot price spreads across every card in the bundle

    Both need the same two properties, and both get them wrong by default:

    1. PRO-RATA BY VALUE, NEVER AN EVEN SPLIT.
       An even split hands a $120 hit the same basis as a $0.50 common. The
       hit then looks like a 2,900% miracle and the commons look like
       disasters, and per-card ROI data is destroyed permanently. Brady's
       Discord lots are his 39%-ROI channel — the channel most worth measuring
       is the one an even split would lie about hardest.

    2. THE PARTS MUST SUM TO THE WHOLE, TO THE PENNY.
       Naive rounding splits $100.00 three ways into $33.33 x3 = $99.99 and
       loses a cent on every bundle. Lost cents mean the books stop balancing
       in a way nobody can reconstruct six months later.

    Rather than keep two copies of that logic in sync — which is exactly how
    profit ended up computed four different ways and all of them wrong — it
    lives here once. Same principle as profit.py and trade_basis.py.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Sequence

CENTS = Decimal("0.01")


def largest_remainder(
    total: Decimal, weights: Sequence[Decimal], refs: Sequence[str]
) -> dict[str, Decimal]:
    """Apportion `total` by `weights` so the parts sum EXACTLY to `total`.

    Floors every share to cents, then distributes the leftover pennies one at
    a time to the largest fractional remainders. Ties break by original order,
    so the result is deterministic and reproducible across runs — a report
    that reshuffles pennies between runs is a report nobody can audit.

    Assumes `weights` sums to more than zero; callers substitute equal weights
    (and say so) when they have no values to go on.
    """
    weight_total = sum(weights, Decimal(0))
    exact = [total * w / weight_total for w in weights]
    floored = [e.quantize(CENTS, rounding="ROUND_DOWN") for e in exact]

    leftover = int(((total - sum(floored, Decimal(0))) / CENTS).to_integral_value())

    order = sorted(range(len(exact)), key=lambda i: (-(exact[i] - floored[i]), i))
    for k in range(leftover):
        floored[order[k % len(order)]] += CENTS

    return dict(zip(refs, floored))
