"""
trade_basis.py

Moves the cost of the cards I trade away onto the cards I get back.

A trade isn't a sale. I don't make or lose money when cards change hands, the
cost just moves from what I gave up to what I got.

In my old spreadsheet I was logging trades as sales that broke even, which
messed up a few things:

    ROI           - a fake $0 profit sale gets averaged into everything
    sell-through  - counts a sale that never happened
    hold time     - stops the clock on a card I basically still have
    cost          - the card I got back shows up with a $0 cost

The last one is the big problem. If a card has a $0 cost, the whole sale price
counts as profit when I sell it, even though I really paid for it months ago.

How it works:

    total_basis = all_in_cost of every card I GAVE + cash_boot

    cash_boot can be positive or negative:
        positive -> I paid cash on top of my cards (adds to the cost)
        negative -> I got cash back (takes away from the cost)

    Then that total gets split across the cards I GOT based on what each one
    is worth, not evenly. If I trade two commons for one big card and split it
    evenly, the big card would barely have any cost and would look like a huge
    win when I sell it.

The split adds up to the total exactly, to the cent (see allocation.py).

No database stuff in here. The caller gets the all_in_cost of the cards I'm
giving up from profit.py and passes the numbers in.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional, Sequence

from app.allocation import largest_remainder

_CENTS = Decimal("0.01")


class TradeBasisError(ValueError):
    """Error for when a trade can't be worked out."""


def _money(value: Any) -> Decimal:
    """Turn a value into a Decimal. Blank or bad values become 0, same as profit.py."""
    if value is None or value == "":
        return Decimal(0)
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 - any parse failure means "no value"
        return Decimal(0)


@dataclass(frozen=True)
class ReceivedCard:
    """A card I'm getting in the trade.

    est_value is about what the card is worth right now. It's only used to
    split up the cost, it isn't saved as a price.
    """

    ref: str
    est_value: Optional[Decimal] = None


@dataclass(frozen=True)
class TradeBasisResult:
    total_basis: Decimal
    allocations: dict[str, Decimal]
    realized_gain: Decimal
    even_split_fallback: bool

    def __post_init__(self) -> None:
        allocated = sum(self.allocations.values(), Decimal(0))
        if allocated != self.total_basis:
            raise TradeBasisError(
                f"allocation {allocated} != total basis {self.total_basis}"
            )


def allocate_trade_basis(
    outgoing_all_in_costs: Sequence[Any],
    received: Sequence[ReceivedCard],
    cash_boot: Any = 0,
) -> TradeBasisResult:
    """Move the cost from the cards I gave up onto the cards I got.

    Returns how much cost each received card gets (by ref). They always add up
    to total_basis exactly.

    realized_gain is usually 0. It only matters if I got back more cash than
    my cards cost me (see below).
    """
    if not received:
        raise TradeBasisError(
            "A trade with no cards received is not a trade — it is a SALE. "
            "Record it through close-out so fees are captured."
        )

    outgoing_total = sum((_money(c) for c in outgoing_all_in_costs), Decimal(0))
    if not outgoing_all_in_costs:
        raise TradeBasisError(
            "A trade with no cards given is not a trade — it is a PURCHASE. "
            "Record it through /add so the cost fields are captured."
        )

    boot = _money(cash_boot)
    total_basis = (outgoing_total + boot).quantize(_CENTS, rounding=ROUND_HALF_UP)

    # Cost can't go below 0. If I got more cash back than my cards cost me,
    # the extra is real profit, so it gets returned as realized_gain instead
    # of just disappearing.
    realized_gain = Decimal(0)
    if total_basis < 0:
        realized_gain = -total_basis
        total_basis = Decimal("0.00")

    weights = [_money(r.est_value) for r in received]
    weight_total = sum(weights, Decimal(0))

    # No values entered, so split it evenly, but flag it so the app can ask me
    # for values. An even split makes the per-card ROI pretty useless.
    even_split_fallback = weight_total <= 0
    if even_split_fallback:
        weights = [Decimal(1)] * len(received)
        weight_total = Decimal(len(received))

    allocations = largest_remainder(total_basis, weights, [r.ref for r in received])

    return TradeBasisResult(
        total_basis=total_basis,
        allocations=allocations,
        realized_gain=realized_gain.quantize(_CENTS),
        even_split_fallback=even_split_fallback,
    )
