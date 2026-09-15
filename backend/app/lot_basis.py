"""
lot_basis.py — split one lot price across the cards inside it. Pure math.

WHY THIS MODULE EXISTS (2026-09-14)
    One payment, many cards. A Discord lot, a collection buy, a repack. The
    receipt says $200; the box has 50 cards. Every card needs a cost basis,
    and the app previously had no way to produce one — verified 2026-09-14,
    there was no `purchase_lots` table and no `lot_id` anywhere in the backend.

    This is the highest-stakes gap in the whole system, because of what
    Brady's own sales history says:

        Cash sales >= $300 (single cards, bought near comp):
            7 cards, $4,048 cost, $232 net  ->   5.7% ROI
        Sub-$300 (Discord LOTS at ~$1.27/card):
            28 cards, $1,294 cost, $504 net ->  39% ROI

    His most profitable channel by a factor of seven is the one the system
    cannot record. Deploying capital into lots without this means every buy in
    the best lane enters with a guessed basis, and the T1 gates (>=60%
    sell-through, >=20% net margin) measure fiction.

THE BULK REMAINDER — the trap that makes naive lot entry worse than useless
    Nobody types in 46 commons. Brady buys a 50-card lot, enters the 4 cards
    worth entering, and the rest goes in a box. If the allocator spreads the
    full $200 across only those 4, each hit absorbs ~$50 of basis it never
    cost — and the lot looks like a loser when it was fine.

    So a lot carries `bulk_remainder_value`: the estimated total value of the
    cards NOT entered individually. It joins the denominator, absorbs its
    proportional share, and that share is recorded on the lot as bulk basis.

        entered card  ->  lot_all_in * est_value / (sum(est_values) + bulk)
        bulk          ->  lot_all_in * bulk      / (sum(est_values) + bulk)

    The books still balance to the penny, and the entered cards carry only
    what they actually cost.

WHAT "lot_all_in" MEANS
    The price of the lot PLUS what it cost to get it: shipping in, tax, and
    any other costs. Same definition of cost as profit.all_in_cost uses for a
    single card — there is one idea of cost in this system, not two.

PURE BY DESIGN
    No database. Same contract as profit.py and trade_basis.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional, Sequence

from app.allocation import largest_remainder

_CENTS = Decimal("0.01")

#: Lot-level cost inputs, mirroring the card-level names in profit.COST_FIELDS.
LOT_COST_FIELDS = ("total_cost", "shipping_in", "purchase_tax", "other_costs")


class LotBasisError(ValueError):
    """Raised when a lot cannot be allocated."""


def _money(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal(0)
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return Decimal(0)


@dataclass(frozen=True)
class LotCard:
    """A card being entered individually from the lot.

    `est_value` is an ALLOCATION WEIGHT — what this card is roughly worth
    relative to the others. It is never a price and never feeds profit.
    """

    ref: str
    est_value: Optional[Decimal] = None


@dataclass(frozen=True)
class LotBasisResult:
    lot_all_in: Decimal
    allocations: dict[str, Decimal]
    bulk_basis: Decimal
    even_split_fallback: bool
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        total = sum(self.allocations.values(), Decimal(0)) + self.bulk_basis
        if total != self.lot_all_in:
            raise LotBasisError(
                f"allocated {total} != lot all-in {self.lot_all_in}"
            )


def lot_all_in_cost(lot: Any) -> Decimal:
    """What the lot actually cost: price + shipping + tax + other."""
    get = lot.get if hasattr(lot, "get") else lambda k, d=None: getattr(lot, k, d)
    return sum((_money(get(f)) for f in LOT_COST_FIELDS), Decimal(0)).quantize(
        _CENTS, rounding=ROUND_HALF_UP
    )


def allocate_lot_basis(
    lot: Any,
    cards: Sequence[LotCard],
    bulk_remainder_value: Any = 0,
) -> LotBasisResult:
    """Spread a lot's all-in cost across its cards, pro-rata by value.

    `bulk_remainder_value` is the estimated total value of cards NOT entered
    individually. Its share of the cost is returned as `bulk_basis` rather
    than being forced onto the entered cards.
    """
    if not cards:
        raise LotBasisError(
            "A lot with no cards entered has nothing to allocate. Enter at "
            "least one card, or record it as a single bulk purchase."
        )

    total = lot_all_in_cost(lot)
    bulk = _money(bulk_remainder_value)
    if bulk < 0:
        raise LotBasisError("bulk_remainder_value cannot be negative.")

    warnings: list[str] = []
    weights = [_money(c.est_value) for c in cards]
    weight_total = sum(weights, Decimal(0))

    even_split_fallback = weight_total <= 0
    if even_split_fallback:
        # No values at all. Fall back to an even split across entered cards
        # and IGNORE the bulk remainder — with no values there is nothing to
        # weigh it against, and inventing a ratio would be worse than saying so.
        weights = [Decimal(1)] * len(cards)
        weight_total = Decimal(len(cards))
        bulk = Decimal(0)
        warnings.append(
            "No estimated values supplied, so the lot cost was split evenly. "
            "This makes every hit look like a miracle and every common look "
            "like a loss — enter rough values to get usable per-card ROI."
        )

    denominator = weight_total + bulk

    # Allocate cards and bulk together so the pennies reconcile across ALL
    # shares, not just the card ones — otherwise the leftover cent lands in
    # bulk every time and the lot slowly drifts out of balance.
    refs = [c.ref for c in cards]
    if bulk > 0:
        shares = largest_remainder(total, weights + [bulk], refs + ["__bulk__"])
        bulk_basis = shares.pop("__bulk__")
        allocations = shares
    else:
        allocations = largest_remainder(total, weights, refs)
        bulk_basis = Decimal("0.00")

    if bulk > 0 and bulk_basis > total / 2:
        warnings.append(
            f"Most of this lot's cost (${bulk_basis} of ${total}) went to cards "
            "you did not enter individually. If any of those are worth "
            "tracking, enter them now — basis cannot be reassigned later."
        )

    return LotBasisResult(
        lot_all_in=total,
        allocations=allocations,
        bulk_basis=bulk_basis,
        even_split_fallback=even_split_fallback,
        warnings=warnings,
    )
