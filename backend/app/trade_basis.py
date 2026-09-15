"""
trade_basis.py — carry-over basis for trades. Pure math, no database.

WHY THIS MODULE EXISTS (2026-09-14)
    A trade is NOT a sale. Nothing is realized when cards change hands; the
    cost basis simply moves from what you gave up to what you received.

    Before this module the app had no concept of a trade at all. The `trades`
    and `trade_items` tables existed in Supabase but NO application code
    touched them — no router, no endpoint, no UI. Trades were being recorded
    in the old spreadsheet as break-even SALES, which corrupts four things at
    once:

        ROI            — a fake $0-profit row averages into every return figure
        sell-through   — a trade counts as a sale that never happened
        hold time      — the clock stops on a card you effectively still hold
        cost basis     — the received card arrives from nowhere, basis $0

    The last one is the expensive one. A card with $0 basis reports its entire
    eventual sale price as profit. Trade into a card and flip it, and the app
    tells you that you made 100% margin on money you actually spent months ago.

THE DEFINITION

    total_basis = sum(all_in_cost of every card GIVEN) + cash_boot

    cash_boot is SIGNED, from Brady's point of view:
        positive  -> Brady PAID cash on top of the cards (adds to basis)
        negative  -> Brady RECEIVED cash (reduces basis)

    That total is then allocated across the cards RECEIVED, pro-rata by their
    estimated market value.

WHY PRO-RATA AND NOT AN EVEN SPLIT
    Identical reasoning to lot allocation. Trade two commons for one big card
    and an even split would hand the big card a trivial basis, so its eventual
    sale looks like a windfall. Pro-rata keeps basis proportional to what each
    card is actually worth, which is the only allocation that leaves per-card
    ROI meaningful.

THE PENNY RULE
    Allocations are reconciled with largest-remainder so the parts sum EXACTLY
    to the total. Naive rounding leaks cents on every multi-card trade, and
    leaked cents mean the books stop balancing in a way nobody can later trace.

PURE BY DESIGN
    No database, no I/O. The caller fetches the outgoing cards' all-in costs
    (via profit.all_in_cost) and passes the numbers in. Same contract as
    profit.py, and unit-testable without a live Supabase.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional, Sequence

from app.allocation import largest_remainder

_CENTS = Decimal("0.01")


class TradeBasisError(ValueError):
    """Raised when a trade cannot be expressed as a carry-over-basis event."""


def _money(value: Any) -> Decimal:
    """Coerce to Decimal money. None/garbage -> 0, matching profit.py."""
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
    """A card coming IN on a trade.

    `est_value` is the card's estimated market value at trade time and is used
    ONLY to apportion basis. It is deliberately not persisted as a price —
    it is an allocation weight, not a valuation claim.
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
    """Carry basis from the cards given up onto the cards received.

    Returns allocations keyed by each ReceivedCard.ref, guaranteed to sum
    exactly to total_basis.

    `realized_gain` is normally 0. It is non-zero only in the boot-exceeds-
    basis case described below, where accounting forces a gain to be
    recognized because basis cannot go negative.
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

    # Basis cannot be negative. If Brady received more cash than the basis he
    # gave up, the excess is a REALIZED GAIN — he has been made whole in cash
    # and then some. Clamping silently would hide real income, so it is
    # returned explicitly for the caller to record.
    realized_gain = Decimal(0)
    if total_basis < 0:
        realized_gain = -total_basis
        total_basis = Decimal("0.00")

    weights = [_money(r.est_value) for r in received]
    weight_total = sum(weights, Decimal(0))

    # No usable values -> even split, but say so. A silent even split is how
    # per-card ROI data gets quietly destroyed; the caller should surface this
    # and ask for values.
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
