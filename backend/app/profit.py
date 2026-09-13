"""
profit.py — THE profit definition. There is exactly one, and it lives here.

WHY THIS MODULE EXISTS (2026-09-13)
    Profit used to be computed in four independent places:

        backend/app/routers/export.py:59    sale_price - purchase_price
        dreamboat-frontend/src/lib/format.ts:27   profitOf(purchase, sale)
        dreamboat-frontend/src/lib/viz.ts:68      revenue - cost
        grading_submissions.cost                  never joined anywhere

    Four copies is how they all drifted into being wrong at the same time: no
    fees, no shipping, no grading, no tax. A $100 -> $200 flip reported +$100
    when the truth was ~$39.50 — a ~2.5x overstatement.

    The fix is not "correct the four formulas." It is "have one formula."
    Every surface consumes what this module returns. The frontend does NOT
    recompute — format.ts and viz.ts render, they do not do arithmetic.

    This matters more than normal because the capital is entirely Brady's own.
    There is no outside investor reviewing the numbers, so this calculation is
    the only thing standing between a losing lane and getting scaled.

THE DEFINITION

    all_in_cost  = purchase_price + shipping_in + purchase_tax + other_costs
                 + grading cost (summed across submissions)

    net_proceeds = sale_price + shipping_collected - platform_fees - shipping_out

    net_profit   = net_proceeds - all_in_cost
    roi          = net_profit / all_in_cost

    shipping_collected is ADDED. It is the shipping fee the customer pays on
    top of the card price — revenue, not cost. Leaving it out was the mirror
    image of the gross-profit bug: it made eBay sales look worse than reality.

TWO RULES THAT ARE EASY TO GET WRONG

    1. An unsold card has net_profit = None, NEVER 0. Zero is a real value that
       averages into ROI and drags it toward nothing; None is excluded. An
       unsold card has not made or lost anything yet.

    2. ROI is None when all_in_cost is 0 — not 0, not infinity. A pulled card
       has no basis, so "return on investment" is undefined rather than
       infinite. Dividing here would raise, and defaulting to 0 would quietly
       pollute every average.

PURE BY DESIGN
    Nothing here touches the database. The caller fetches grading costs and
    passes them in. That keeps the math unit-testable without a live Supabase.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Optional

# Money is rounded to cents on output; ROI keeps more precision because it is a
# ratio that gets formatted as a percentage downstream.
_CENTS = Decimal("0.01")
_RATIO = Decimal("0.000001")

# Fields summed into all_in_cost. Named here so a future cost column is added in
# exactly one place and cannot be forgotten by one of the callers.
COST_FIELDS = ("purchase_price", "shipping_in", "purchase_tax", "other_costs")

# Fields that make up net_proceeds. Sign matters: collected shipping is revenue.
PROCEEDS_ADD = ("sale_price", "shipping_collected")
PROCEEDS_SUB = ("platform_fees", "shipping_out")

SOLD_STATUS = "sold"


def _money(value: Any) -> Decimal:
    """Coerce a DB/JSON value to Decimal, treating null and junk as 0.

    Supabase returns numerics as strings or floats depending on the client and
    column, so this normalises both. float is routed through str to avoid
    binary-float artefacts like 0.1 + 0.2 showing up in a money total.
    """
    if value is None or value == "":
        return Decimal(0)
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(0)


def _opt_money(value: Any) -> Optional[Decimal]:
    """Like _money, but preserves the difference between 'absent' and 'zero'."""
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _as_date(value: Any) -> Optional[date]:
    """Parse a date from the DB, which may hand back date, datetime, or str."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        # Handles both "2026-09-13" and full ISO timestamps.
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None


def is_sold(card: Mapping[str, Any]) -> bool:
    """A card counts as sold only on the explicit status.

    Deliberately NOT "has a sale_price". A card can carry a sale_price while
    sitting in another status, and `traded_away` is a separate status precisely
    because a trade is not a sale — it must never book revenue.
    """
    return (card.get("status") or "").lower() == SOLD_STATUS


def grading_cost_for(submissions: Optional[Iterable[Mapping[str, Any]]]) -> Decimal:
    """Sum the cost of a card's grading submissions.

    THIS IS THE JOIN EVERYONE FORGETS. `grading_submissions.cost` has existed
    since the initial schema and was never once included in a profit number.
    A card can have several submissions (resubmits, crossovers), so it sums
    rather than taking one.
    """
    if not submissions:
        return Decimal(0)
    return sum((_money(s.get("cost")) for s in submissions), Decimal(0))


def all_in_cost(
    card: Mapping[str, Any],
    grading_cost: Decimal = Decimal(0),
) -> Decimal:
    """Everything spent to get this card into hand and ready to sell."""
    total = sum((_money(card.get(f)) for f in COST_FIELDS), Decimal(0))
    return (total + _money(grading_cost)).quantize(_CENTS)


def net_proceeds(card: Mapping[str, Any]) -> Optional[Decimal]:
    """What actually landed from the sale. None if the card is not sold."""
    if not is_sold(card):
        return None
    total = sum((_money(card.get(f)) for f in PROCEEDS_ADD), Decimal(0))
    total -= sum((_money(card.get(f)) for f in PROCEEDS_SUB), Decimal(0))
    return total.quantize(_CENTS)


def net_profit(
    card: Mapping[str, Any],
    grading_cost: Decimal = Decimal(0),
) -> Optional[Decimal]:
    """Realized profit. None until sold — never 0. See module docstring."""
    proceeds = net_proceeds(card)
    if proceeds is None:
        return None
    return (proceeds - all_in_cost(card, grading_cost)).quantize(_CENTS)


def roi(
    card: Mapping[str, Any],
    grading_cost: Decimal = Decimal(0),
) -> Optional[Decimal]:
    """net_profit / all_in_cost, as a ratio (0.395 == +39.5%).

    None when unsold, and None when all_in_cost is 0 (undefined, not infinite).
    """
    profit = net_profit(card, grading_cost)
    if profit is None:
        return None
    basis = all_in_cost(card, grading_cost)
    if basis == 0:
        return None
    return (profit / basis).quantize(_RATIO)


def hold_days(
    card: Mapping[str, Any],
    today: Optional[date] = None,
) -> Optional[int]:
    """Days the card has been (or was) held.

    Sold   -> sale_date - purchase_date  (the clock stopped)
    Unsold -> today - purchase_date      (the clock is running)

    None when purchase_date is missing. That is a real reporting hole, not a
    cosmetic one: a card with no purchase_date has no age and drops out of
    every aging bucket silently. Manual entry must require purchase_date.
    """
    start = _as_date(card.get("purchase_date"))
    if start is None:
        return None
    end = _as_date(card.get("sale_date")) if is_sold(card) else (today or date.today())
    if end is None:
        end = today or date.today()
    return (end - start).days


def enrich(
    card: Mapping[str, Any],
    submissions: Optional[Iterable[Mapping[str, Any]]] = None,
    today: Optional[date] = None,
) -> dict:
    """Return the card dict plus the computed fields every surface reads.

    Adds: all_in_cost, net_proceeds, net_profit, roi, hold_days, grading_cost.
    The original keys are untouched, so this is safe to hand straight to the
    response model.
    """
    gcost = grading_cost_for(submissions)
    enriched = dict(card)
    enriched.update(
        {
            "grading_cost": gcost.quantize(_CENTS),
            "all_in_cost": all_in_cost(card, gcost),
            "net_proceeds": net_proceeds(card),
            "net_profit": net_profit(card, gcost),
            "roi": roi(card, gcost),
            "hold_days": hold_days(card, today),
        }
    )
    return enriched


def enrich_many(
    cards: Iterable[Mapping[str, Any]],
    submissions_by_card: Optional[Mapping[str, Iterable[Mapping[str, Any]]]] = None,
    today: Optional[date] = None,
) -> list[dict]:
    """Batch form of `enrich`.

    `submissions_by_card` maps card_id -> that card's grading submissions, so
    the caller can fetch them in ONE query instead of N. Pass None when grading
    costs are not needed (they resolve to 0).
    """
    lookup = submissions_by_card or {}
    stamp = today or date.today()
    return [enrich(c, lookup.get(str(c.get("id"))), stamp) for c in cards]
