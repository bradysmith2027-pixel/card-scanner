"""
profit.py

All of the profit math lives here and nowhere else.

Profit used to get calculated in a few different places (the CSV export and a
couple spots in the frontend) and none of them took out fees, shipping, or
grading. So a $100 card I sold for $200 showed +$100 when I really made closer
to $40. Now everything just uses what this file returns, and the frontend only
displays it.

How the numbers work:

    all_in_cost  = purchase_price + shipping_in + purchase_tax + other_costs
                 + grading cost (all submissions added up)

    net_proceeds = sale_price + shipping_collected - platform_fees - shipping_out

    net_profit   = net_proceeds - all_in_cost
    roi          = net_profit / all_in_cost

shipping_collected gets added, not subtracted. It's what the buyer pays me for
shipping, so it counts as money coming in.

Two rules:

    1. If a card isn't sold yet, net_profit is None, not 0. A 0 would get
       averaged into ROI and drag it down even though nothing has happened yet.

    2. If all_in_cost is 0 (like a card I pulled from a pack), roi is None.
       Can't divide by 0, and putting 0 there would mess up the averages.

Nothing in here touches the database. The caller looks up the grading costs and
passes them in, which makes this easy to test.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Optional

# Money gets rounded to cents. ROI keeps more decimals since it gets turned
# into a percentage later.
_CENTS = Decimal("0.01")
_RATIO = Decimal("0.000001")

# Everything that adds up to all_in_cost. If I add a new cost column, it only
# has to go here.
COST_FIELDS = ("purchase_price", "shipping_in", "purchase_tax", "other_costs")

# What makes up net_proceeds. Shipping the buyer paid me counts as money in.
PROCEEDS_ADD = ("sale_price", "shipping_collected")
PROCEEDS_SUB = ("platform_fees", "shipping_out")

SOLD_STATUS = "sold"


def _money(value: Any) -> Decimal:
    """Turn a value from the database into a Decimal. Blank or bad values become 0.

    Supabase sometimes sends numbers back as strings and sometimes as floats,
    so this handles both. Floats go through str() first so I don't get weird
    float rounding showing up in money totals.
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
    """Same as _money, but keeps None as None instead of turning it into 0."""
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _as_date(value: Any) -> Optional[date]:
    """Get a date out of whatever the database sends (date, datetime, or a string)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        # Works for "2026-09-13" and full timestamps.
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None


def is_sold(card: Mapping[str, Any]) -> bool:
    """A card only counts as sold if its status says sold.

    Having a sale_price isn't enough. Traded cards have their own status
    (traded_away) because a trade isn't a sale and shouldn't count as revenue.
    """
    return (card.get("status") or "").lower() == SOLD_STATUS


def grading_cost_for(submissions: Optional[Iterable[Mapping[str, Any]]]) -> Decimal:
    """Add up what I paid to grade a card.

    This cost was never included in profit before, which is part of why the old
    numbers were too high. A card can be sent in more than once (resubmits,
    crossovers), so it adds them all up.
    """
    if not submissions:
        return Decimal(0)
    return sum((_money(s.get("cost")) for s in submissions), Decimal(0))


def all_in_cost(
    card: Mapping[str, Any],
    grading_cost: Decimal = Decimal(0),
) -> Decimal:
    """Everything I spent to get the card in hand and ready to sell."""
    total = sum((_money(card.get(f)) for f in COST_FIELDS), Decimal(0))
    return (total + _money(grading_cost)).quantize(_CENTS)


def net_proceeds(card: Mapping[str, Any]) -> Optional[Decimal]:
    """What I actually took home from the sale. None if it isn't sold."""
    if not is_sold(card):
        return None
    total = sum((_money(card.get(f)) for f in PROCEEDS_ADD), Decimal(0))
    total -= sum((_money(card.get(f)) for f in PROCEEDS_SUB), Decimal(0))
    return total.quantize(_CENTS)


def net_profit(
    card: Mapping[str, Any],
    grading_cost: Decimal = Decimal(0),
) -> Optional[Decimal]:
    """Actual profit on the card. None until it sells, never 0."""
    proceeds = net_proceeds(card)
    if proceeds is None:
        return None
    return (proceeds - all_in_cost(card, grading_cost)).quantize(_CENTS)


def roi(
    card: Mapping[str, Any],
    grading_cost: Decimal = Decimal(0),
) -> Optional[Decimal]:
    """net_profit / all_in_cost as a decimal (0.395 means +39.5%).

    None if the card isn't sold or if it cost me nothing.
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
    """How many days I've had the card (or had it before selling).

    Sold   -> sale_date - purchase_date
    Unsold -> today - purchase_date

    None if there's no purchase_date. That card then won't show up in any of
    the aging numbers, which is why the add card form makes the date required.
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
    """Return the card with all the calculated numbers added on.

    Adds all_in_cost, net_proceeds, net_profit, roi, hold_days and
    grading_cost. The original fields stay the same, so the result can go
    straight into the response.
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
    """Same as enrich, but for a list of cards.

    submissions_by_card maps each card id to its grading submissions, so the
    caller can grab them all in one query instead of one per card. Pass None if
    grading costs don't matter (they'll count as 0).
    """
    lookup = submissions_by_card or {}
    stamp = today or date.today()
    return [enrich(c, lookup.get(str(c.get("id"))), stamp) for c in cards]
