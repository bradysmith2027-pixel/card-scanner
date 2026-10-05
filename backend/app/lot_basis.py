"""
lot_basis.py

Splits the price of a lot across the cards in it.

When I buy a lot (Discord lot, a collection, a repack) I pay one price for a
bunch of cards. Say $200 for 50 cards. Every card still needs its own cost so
profit works when I sell it.

My lots have been my best ROI by far (around 39% vs about 6% on single cards
I buy near comps), so I want these costs to actually be right.

The bulk remainder:
    I'm not going to type in 46 commons. I enter the 4 cards that matter and
    the rest go in a box. If the whole $200 got split across just those 4,
    each one would carry about $50 of cost it never really had, and the lot
    would look like a loss when it wasn't.

    So a lot has a bulk_remainder_value, which is my guess at what all the
    cards I didn't enter are worth together. It gets its share of the cost
    like any other card:

        entered card  ->  lot_all_in * est_value / (sum(est_values) + bulk)
        bulk          ->  lot_all_in * bulk      / (sum(est_values) + bulk)

    Everything still adds up to the cent, and the cards I entered only carry
    what they really cost.

lot_all_in means the lot price plus shipping, tax and any other costs. Same
idea as all_in_cost in profit.py for a single card.

No database stuff in here, same as profit.py and trade_basis.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional, Sequence

from app.allocation import largest_remainder

_CENTS = Decimal("0.01")

# The lot's cost fields. Same idea as COST_FIELDS in profit.py.
LOT_COST_FIELDS = ("total_cost", "shipping_in", "purchase_tax", "other_costs")


class LotBasisError(ValueError):
    """Error for when a lot can't be split up."""


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
    """A card from the lot that I'm entering on its own.

    est_value is just how much the card is worth compared to the others, so
    the cost can be split. It's not a price and doesn't go into profit.
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
    """What the lot really cost: price + shipping + tax + other."""
    get = lot.get if hasattr(lot, "get") else lambda k, d=None: getattr(lot, k, d)
    return sum((_money(get(f)) for f in LOT_COST_FIELDS), Decimal(0)).quantize(
        _CENTS, rounding=ROUND_HALF_UP
    )


def allocate_lot_basis(
    lot: Any,
    cards: Sequence[LotCard],
    bulk_remainder_value: Any = 0,
) -> LotBasisResult:
    """Split a lot's total cost across its cards based on what each is worth.

    bulk_remainder_value is roughly what all the cards I didn't enter are worth.
    Their share of the cost comes back as bulk_basis so it doesn't get pushed
    onto the cards I did enter.
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
        # No values entered at all, so split it evenly across the entered
        # cards and ignore the bulk remainder. With no values there's nothing
        # to compare the bulk against.
        weights = [Decimal(1)] * len(cards)
        weight_total = Decimal(len(cards))
        bulk = Decimal(0)
        warnings.append(
            "No estimated values supplied, so the lot cost was split evenly. "
            "This makes every hit look like a miracle and every common look "
            "like a loss — enter rough values to get usable per-card ROI."
        )

    denominator = weight_total + bulk

    # Split the cards and the bulk together so the leftover pennies get handed
    # out fairly. Otherwise the extra cent would always end up on the bulk.
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
