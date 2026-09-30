"""Weekly Ops report — computed from the database, never hand-assembled.

`04 Dreamboat Slabs/[C] Reporting Procedure.md` states the rule this module
enforces: *"every report is generated from the database, never hand-assembled.
A report typed by hand is a report that quietly flatters itself."*

Design rules, all of them load-bearing:

1. **This module never does profit math.** Every money figure comes from
   `profit.enrich_many`. Six drifting copies of the profit formula is the
   mistake this codebase already made once (found 2026-09-11, fixed 09-13).
2. **Pure functions.** No database, no network, no email. Rows in, report
   object out — so the whole thing is testable without a live Supabase.
3. **A gap is reported as a gap, never omitted and never faked.** Anything
   that cannot be computed says so in the output.
4. **Row counts travel with the report**, so a truncated query is visible
   instead of silently producing a rosy week.
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

#: Aging thresholds, in days held. A card is reported when it CROSSES one of
#: these inside the reporting window, not every week thereafter — a report that
#: repeats the same twelve cards every Sunday stops being read by week three.
AGING_THRESHOLDS = (90, 180)

#: Channels that always deduct a fee at sale.
#:
#: 🔴 This is deliberately a small allowlist rather than "flag every zero."
#: `platform_fees` is `NOT NULL DEFAULT 0`, so a zero is not a null and does
#: not by itself mean data is missing. The 2026-09-17 import audit settled
#: this: Brady's position was *"I would include fees if they had them,"* and
#: the data agreed — 13 of 14 eBay sales carried a fee at credible rates,
#: while cash and in-person sales legitimately carry none. Flagging every
#: zero would have produced 23 false alarms and one real one, which is how a
#: report trains you to ignore it.
#:
#: Exactly one genuine gap existed (row 72, Booster Boxes, $341 on eBay —
#: whose missing fee flips it from +$41 to about -$5). This rule finds that
#: row and leaves the other 22 alone.
FEE_CHARGING_CHANNELS = frozenset({"ebay"})

#: 🔴 THE MONTHLY REPORT MUST PRINT THIS. The monthly review has a capital
#: section (cash available, allocation vs 70/15/15, tranche status) and none of
#: it is computable until the `capital_events` ledger exists. It has to say so
#: rather than substituting cost-of-inventory, which differs from capital
#: deployed by whatever cash is on hand — a plausible wrong number in a
#: self-reviewed report is the exact failure this reporting system exists to
#: prevent, and there is no outside investor to catch it.
#:
#: It is deliberately NOT on the WEEKLY report (removed 2026-09-30, Brady's
#: call). The weekly has no capital section for it to qualify, so it was
#: unchanging boilerplate on every send — and a notice that never changes is
#: one readers learn to skip, which would blunt the gaps that DO vary.
CAPITAL_UNAVAILABLE = (
    "Capital position (cash available, allocation vs 70/15/15, tranche status): "
    "NOT AVAILABLE — requires the capital_events ledger (Phase 2)."
)


def _money(value: Any) -> Decimal:
    """Coerce to Decimal. None and unparseable values become 0."""
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
    """Human-readable card name for a report line.

    ⚠️ The column is `player`, NOT `player_name` — a trap the migrations never
    described and the 9/17 import hit head-on.
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
    """The window a report covers: [start, end), `days` long, ending today.

    Half-open on purpose, and the boundaries are worth stating exactly because
    off-by-one here either double-counts a sale or loses one:

        a sale dated `start`  -> IN this report   (start is inclusive)
        a sale dated `end`    -> NEXT report      (end is exclusive)

    `end` is today, so a sale closed later today lands in next week's report.
    That is the right behaviour for a Sunday-morning report on the week that
    just finished, and it guarantees consecutive windows tile perfectly: no
    sale is ever counted twice or dropped between them.
    """
    return (report_date - timedelta(days=days), report_date)


def _in_window(when: Optional[date], start: date, end: date) -> bool:
    return when is not None and start <= when < end


@dataclass
class Line:
    """One named card on a report, with the single number that earned it a line."""

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

    #: Row counts the report was built from, so truncation is visible.
    source_counts: dict[str, int] = field(default_factory=dict)
    #: Things that could not be computed, stated rather than hidden.
    gaps: list[str] = field(default_factory=list)

    @property
    def quiet(self) -> bool:
        """True when nothing at all happened. Still gets sent."""
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

    `cards` is every card row for the user — not a pre-filtered set. Filtering
    happens here so the row counts in the output mean something.
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

        # --- purchases logged this week ---
        if _in_window(bought, start, end):
            cost = _money(card.get("all_in_cost"))
            report.purchases.append(Line(cid, _label(card), cost))
            report.purchase_cost += cost

        # --- sales closed this week ---
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

                # fee gap — only where the channel always charges one
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

        # --- aging: crossed a threshold inside this window ---
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

        # --- capital in flight ---
        if status == IN_TRANSIT_STATUS:
            report.in_transit_count += 1
            report.in_transit_dollars += _money(card.get("all_in_cost"))

    if report.sales:
        report.best_sale = max(report.sales, key=lambda l: l.amount or ZERO)
        report.worst_sale = min(report.sales, key=lambda l: l.amount or ZERO)

    # --- trailing average, for context on this week's number ---
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

    # 🔴 The capital disclaimer is NOT added here — see CAPITAL_UNAVAILABLE.
    return report


def _fmt(amount: Optional[Decimal]) -> str:
    if amount is None:
        return "n/a"
    q = amount.quantize(CENTS)
    sign = "-" if q < 0 else ""
    return f"{sign}${abs(q):,.2f}"


def render_text(report: WeeklyReport) -> str:
    """Plain-text rendering. The HTML mail body is built from the same data."""
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
