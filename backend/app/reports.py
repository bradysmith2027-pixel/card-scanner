"""Weekly Ops report, built straight from the database.

My rule for reports is that they always come from the database and never get
put together by hand. A report I type up myself is going to end up making
things look better than they are.

How this file works:

1. No profit math in here. Every money number comes from profit.enrich_many.
   I already had profit calculated in six different places once and they all
   ended up wrong.
2. No database, network or email in here either. Cards go in, a report comes
   out, so it's easy to test.
3. If something can't be calculated, the report says so. It doesn't skip it
   or make up a number.
4. The report includes how many rows it used, so if a query got cut off I can
   tell instead of getting a week that looks too good.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping, Optional

from . import profit

CENTS = Decimal("0.01")
ZERO = Decimal("0")

IN_TRANSIT_STATUS = "in_transit"

# Aging cutoffs in days. A card shows up the week it crosses one of these, not
# every week after. If the same twelve cards showed up every Sunday I'd stop
# reading it.
AGING_THRESHOLDS = (90, 180)

# Places that always take a fee when something sells.
#
# I only flag a $0 fee on these, not every $0 fee. platform_fees defaults to 0,
# and a 0 usually just means there wasn't a fee (cash, in person, Discord). I
# always enter fees when there are some. When I imported my spreadsheet, 13 of
# 14 eBay sales had a fee, and flagging every 0 would have been 23 false alarms
# for 1 real one.
#
# The one real one was a $341 eBay sale of booster boxes with no fee entered,
# which takes it from about +$41 to about -$5. This catches that one and
# leaves the rest alone.
FEE_CHARGING_CHANNELS = frozenset({"ebay"})

# The monthly report needs to show this. It has a capital section (cash on
# hand, the 70/15/15 split, tranche status) and none of that can be calculated
# until I build the capital_events table. Until then it should say that instead
# of using inventory cost, which isn't the same thing as how much money I've
# put in. Nobody else is checking these numbers, so a wrong number that looks
# right is the worst thing it could show.
#
# I took it off the weekly report. The weekly doesn't have a capital section,
# so it was just the same note every week, and I'd start skipping that part
# and miss the notes that actually change.
CAPITAL_UNAVAILABLE = (
    "Capital position (cash available, allocation vs 70/15/15, tranche status): "
    "NOT AVAILABLE — requires the capital_events ledger (Phase 2)."
)


def _money(value: Any) -> Decimal:
    """Turn a value into a Decimal. Blank or bad values become 0."""
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return ZERO


def _as_date(value: Any) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except Exception:
        return None


def _label(card: Mapping[str, Any]) -> str:
    """The card's name for a line in the report.

    The column is player, not player_name. That tripped me up during the
    spreadsheet import.
    """
    bits = [
        str(card.get("year") or "").strip(),
        str(card.get("set_name") or "").strip(),
        str(card.get("player") or "").strip(),
    ]
    name = " ".join(b for b in bits if b)
    serial = str(card.get("serial") or "").strip()
    if serial:
        name = f"{name} /{serial}" if not serial.startswith("/") else f"{name} {serial}"
    return name or f"card {card.get('id')}"


def week_bounds(report_date: date, days: int = 7) -> tuple[date, date]:
    """The dates a report covers: from start up to (not including) end.

    Getting this off by one day would count a sale twice or miss it, so:

        a sale on start  -> in this report
        a sale on end    -> in next week's report

    end is today, so anything I sell later today shows up next week. That
    works for a Sunday morning report on the week before, and it means no
    sale ever gets counted twice or skipped between weeks.
    """
    return (report_date - timedelta(days=days), report_date)


def _in_window(when: Optional[date], start: date, end: date) -> bool:
    return when is not None and start <= when < end


@dataclass
class Line:
    """One card on the report, with the number that got it there."""

    card_id: str
    label: str
    amount: Optional[Decimal] = None
    detail: str = ""


@dataclass
class WeeklyReport:
    period_start: date
    period_end: date

    purchases: list[Line] = field(default_factory=list)
    purchase_cost: Decimal = ZERO

    sales: list[Line] = field(default_factory=list)
    realized_profit: Decimal = ZERO
    prior_4wk_average: Optional[Decimal] = None

    best_sale: Optional[Line] = None
    worst_sale: Optional[Line] = None

    fee_gaps: list[Line] = field(default_factory=list)
    aging: dict[int, list[Line]] = field(default_factory=dict)

    in_transit_count: int = 0
    in_transit_dollars: Decimal = ZERO

    # How many rows the report used, so I can tell if something got cut off.
    source_counts: dict[str, int] = field(default_factory=dict)
    # Anything that couldn't be calculated, so it shows up instead of being hidden.
    gaps: list[str] = field(default_factory=list)

    @property
    def quiet(self) -> bool:
        """True if nothing happened this week. It still gets sent."""
        return not self.purchases and not self.sales


def _realized_between(cards: list[dict], start: date, end: date) -> Decimal:
    total = ZERO
    for c in cards:
        if not profit.is_sold(c):
            continue
        if _in_window(_as_date(c.get("sale_date")), start, end):
            total += _money(c.get("net_profit"))
    return total


def build_weekly(
    cards: Iterable[Mapping[str, Any]],
    submissions_by_card: Optional[Mapping[str, Iterable[Mapping[str, Any]]]] = None,
    today: Optional[date] = None,
    window_days: int = 7,
    trailing_weeks: int = 4,
) -> WeeklyReport:
    """Build the Weekly Ops report.

    cards is every card for the user, not a filtered list. The filtering
    happens in here so the row counts actually mean something.
    """
    report_date = today or date.today()
    start, end = week_bounds(report_date, window_days)

    enriched = profit.enrich_many(cards, submissions_by_card, report_date)

    report = WeeklyReport(period_start=start, period_end=end)

    no_purchase_date = 0
    sold_total = 0

    for card in enriched:
        cid = str(card.get("id"))
        status = (card.get("status") or "").lower()
        bought = _as_date(card.get("purchase_date"))
        sold = profit.is_sold(card)

        if bought is None:
            no_purchase_date += 1

        # --- cards bought this week ---
        if _in_window(bought, start, end):
            cost = _money(card.get("all_in_cost"))
            report.purchases.append(Line(cid, _label(card), cost))
            report.purchase_cost += cost

        # --- cards sold this week ---
        if sold:
            sold_total += 1
            sale_day = _as_date(card.get("sale_date"))
            if _in_window(sale_day, start, end):
                net = _money(card.get("net_profit"))
                line = Line(
                    cid,
                    _label(card),
                    net,
                    detail=f"held {card.get('hold_days')}d",
                )
                report.sales.append(line)
                report.realized_profit += net

                # missing fee, only for places that always charge one
                channel = (card.get("sale_channel") or "").lower()
                if channel in FEE_CHARGING_CHANNELS and _money(
                    card.get("platform_fees")
                ) == ZERO:
                    report.fee_gaps.append(
                        Line(
                            cid,
                            _label(card),
                            _money(card.get("sale_price")),
                            detail=f"{channel} sale with no platform fee recorded",
                        )
                    )

        # --- aging: cards that crossed a cutoff this week ---
        if not sold and bought is not None:
            held = card.get("hold_days")
            if isinstance(held, int):
                for threshold in AGING_THRESHOLDS:
                    if held >= threshold > held - window_days:
                        report.aging.setdefault(threshold, []).append(
                            Line(
                                cid,
                                _label(card),
                                _money(card.get("all_in_cost")),
                                detail=f"{held} days held",
                            )
                        )

        # --- money tied up in cards in transit or at grading ---
        if status == IN_TRANSIT_STATUS:
            report.in_transit_count += 1
            report.in_transit_dollars += _money(card.get("all_in_cost"))

    if report.sales:
        report.best_sale = max(report.sales, key=lambda l: l.amount or ZERO)
        report.worst_sale = min(report.sales, key=lambda l: l.amount or ZERO)

    # --- average over the last few weeks, to compare this week to ---
    prior: list[Decimal] = []
    for i in range(1, trailing_weeks + 1):
        w_end = start - timedelta(days=window_days * (i - 1))
        w_start = w_end - timedelta(days=window_days)
        prior.append(_realized_between(enriched, w_start, w_end))
    if prior:
        report.prior_4wk_average = (sum(prior, ZERO) / len(prior)).quantize(CENTS)

    report.source_counts = {
        "cards_scanned": len(enriched),
        "sold_all_time": sold_total,
        "unsold": len(enriched) - sold_total,
        "missing_purchase_date": no_purchase_date,
    }

    if no_purchase_date:
        report.gaps.append(
            f"{no_purchase_date} card(s) have no purchase_date, so they have no age "
            "and are invisible to the aging section."
        )

    # The capital note doesn't go here on purpose, see CAPITAL_UNAVAILABLE.
    return report


def _fmt(amount: Optional[Decimal]) -> str:
    if amount is None:
        return "n/a"
    q = amount.quantize(CENTS)
    sign = "-" if q < 0 else ""
    return f"{sign}${abs(q):,.2f}"


def render_text(report: WeeklyReport) -> str:
    """Plain text version. The HTML email uses the same data."""
    out: list[str] = []
    add = out.append

    add(f"DREAMBOAT SLABS — WEEKLY OPS")
    add(f"{report.period_start.isoformat()} to {report.period_end.isoformat()}")
    add("")

    if report.quiet:
        add("No purchases and no sales this week.")
        add("(Sent anyway — a silent week and a broken job look identical.)")
        add("")

    add(f"PURCHASES ({len(report.purchases)})  {_fmt(report.purchase_cost)} deployed")
    for line in report.purchases:
        add(f"  - {line.label}: {_fmt(line.amount)}")
    add("")

    add(f"SALES CLOSED ({len(report.sales)})  net {_fmt(report.realized_profit)}")
    for line in report.sales:
        add(f"  - {line.label}: {_fmt(line.amount)} ({line.detail})")
    if report.prior_4wk_average is not None:
        add(f"  prior 4-week average: {_fmt(report.prior_4wk_average)}")
    if report.best_sale:
        add(f"  best:  {report.best_sale.label} {_fmt(report.best_sale.amount)}")
    if report.worst_sale:
        add(f"  worst: {report.worst_sale.label} {_fmt(report.worst_sale.amount)}")
    add("")

    if report.fee_gaps:
        add(f"!! FEES MISSING ({len(report.fee_gaps)}) — fix these now")
        for line in report.fee_gaps:
            add(f"  - {line.label}: {_fmt(line.amount)} — {line.detail}")
        add("  A fee not captured at close-out is never captured.")
        add("")

    for threshold in AGING_THRESHOLDS:
        lines = report.aging.get(threshold) or []
        if lines:
            add(f"CROSSED {threshold} DAYS HELD ({len(lines)})")
            for line in lines:
                add(f"  - {line.label}: {_fmt(line.amount)} ({line.detail})")
            add("")

    add(
        f"IN TRANSIT: {report.in_transit_count} card(s), "
        f"{_fmt(report.in_transit_dollars)} of capital in flight"
    )
    add("")

    add("BUILT FROM")
    for key, value in report.source_counts.items():
        add(f"  {key}: {value}")
    add("")

    if report.gaps:
        add("NOT COMPUTED")
        for gap in report.gaps:
            add(f"  - {gap}")

    return "\n".join(out)
