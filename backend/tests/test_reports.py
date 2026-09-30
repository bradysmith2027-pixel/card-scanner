"""test_reports.py — the Weekly Ops report generator.

The report is pure input -> output, so it has no excuse for being untested.

The most important test in this file is the NON-VACUOUS CONTROL
(`test_fee_gap_changes_the_report`): a fixture where a fee is missing must
produce a *different* report than one where it is not. The profit bug survived
for months because the old test asserted `250 - 100 == 150`, which still passes
under the correct code — every fixture omitted fees, so gross equalled net and
the assertion could not tell the two apart. Same trap, different module.
"""

from datetime import date
from decimal import Decimal

import pytest

from app import reports

pytestmark = pytest.mark.unit

TODAY = date(2026, 9, 28)


def card(**over):
    """A sane in-hand card. Override only what a test is about."""
    base = {
        "id": "c1",
        "player": "Test Player",
        "set_name": "Prizm",
        "year": "2023",
        "status": "in_hand",
        "purchase_date": "2026-09-25",
        "purchase_price": "100",
        "shipping_in": "0",
        "purchase_tax": "0",
        "other_costs": "0",
        "platform_fees": "0",
        "shipping_out": "0",
        "shipping_collected": "0",
    }
    base.update(over)
    return base


def sold(**over):
    """The exact scenario verified against the live database on 2026-09-13.

    $100 card + $13 other costs = $113 all-in. Sold $200, $26 fees, $5 shipped
    out, $6 shipping collected -> net proceeds $175 -> +$62.00 net profit.
    The old gross code reported +$100.00 on this same row.
    """
    base = card(
        status="sold",
        purchase_date="2026-09-01",
        other_costs="13",
        sale_date="2026-09-25",
        sale_price="200",
        platform_fees="26",
        shipping_out="5",
        shipping_collected="6",
    )
    base.update(over)
    return base


# --------------------------------------------------------------------------
# Window arithmetic
# --------------------------------------------------------------------------
def test_window_is_half_open_so_nothing_is_double_counted():
    start, end = reports.week_bounds(TODAY)
    assert start == date(2026, 9, 21)
    assert end == TODAY

    # start is INCLUSIVE — a sale on the first day is in this report.
    r = reports.build_weekly([sold(sale_date="2026-09-21")], today=TODAY)
    assert len(r.sales) == 1

    # the day before start belongs to the PREVIOUS report.
    r = reports.build_weekly([sold(sale_date="2026-09-20")], today=TODAY)
    assert r.sales == []

    # end is EXCLUSIVE — a sale closed later today lands in NEXT week's report.
    r = reports.build_weekly([sold(sale_date="2026-09-28")], today=TODAY)
    assert r.sales == []

    # and the two windows tile: that same sale is picked up a week later.
    r = reports.build_weekly(
        [sold(sale_date="2026-09-28")], today=date(2026, 10, 5)
    )
    assert len(r.sales) == 1


# --------------------------------------------------------------------------
# The report never does its own profit math
# --------------------------------------------------------------------------
def test_realized_profit_is_net_not_gross():
    """$100 all-in, $200 sale, $26 fees, $5 ship out, $6 collected -> +$62.

    If this ever reads 100.00 the report has started computing gross profit,
    which is the exact bug that cost this project two and a half months of
    wrong numbers.
    """
    r = reports.build_weekly([sold()], today=TODAY)
    assert r.realized_profit == Decimal("62.00")


# --------------------------------------------------------------------------
# 🔴 THE NON-VACUOUS CONTROL
# --------------------------------------------------------------------------
def test_fee_gap_changes_the_report():
    """A missing fee must make the report DIFFERENT, not just differently worded."""
    with_fee = reports.build_weekly(
        [sold(sale_channel="ebay", platform_fees="26")], today=TODAY
    )
    without_fee = reports.build_weekly(
        [sold(sale_channel="ebay", platform_fees="0")], today=TODAY
    )

    assert with_fee.fee_gaps == []
    assert len(without_fee.fee_gaps) == 1
    # and the money moves too, not just the warning list
    assert without_fee.realized_profit != with_fee.realized_profit
    assert reports.render_text(without_fee) != reports.render_text(with_fee)
    assert "FEES MISSING" in reports.render_text(without_fee)
    assert "FEES MISSING" not in reports.render_text(with_fee)


def test_zero_fee_on_a_cash_channel_is_not_a_gap():
    """The 2026-09-17 finding, encoded.

    Blank fees were a FALSE ALARM on 22 of 23 rows — a cash or in-person sale
    legitimately carries no platform fee. Flagging every zero is how a weekly
    report becomes noise and stops being read.
    """
    for channel in ("show", "discord", "facebook", "instagram"):
        r = reports.build_weekly(
            [sold(sale_channel=channel, platform_fees="0")], today=TODAY
        )
        assert r.fee_gaps == [], f"{channel} should not be flagged"


def test_missing_channel_is_not_flagged():
    """No channel recorded is not evidence of a missing fee."""
    r = reports.build_weekly([sold(sale_channel=None, platform_fees="0")], today=TODAY)
    assert r.fee_gaps == []


# --------------------------------------------------------------------------
# Aging
# --------------------------------------------------------------------------
def test_aging_reports_only_the_week_a_threshold_is_crossed():
    """A card 200 days old must not appear in the 90-day list every single week."""
    just_crossed = card(id="new90", purchase_date="2026-06-29")  # 91 days
    long_past = card(id="old", purchase_date="2026-01-01")  # ~270 days

    r = reports.build_weekly([just_crossed, long_past], today=TODAY)

    ninety = [l.card_id for l in r.aging.get(90, [])]
    assert "new90" in ninety
    assert "old" not in ninety


def test_sold_cards_do_not_age():
    r = reports.build_weekly(
        [sold(id="s", purchase_date="2026-06-29", sale_date="2026-09-25")], today=TODAY
    )
    assert r.aging.get(90) is None


# --------------------------------------------------------------------------
# Capital in flight
# --------------------------------------------------------------------------
def test_in_transit_dollars_are_summed():
    r = reports.build_weekly(
        [
            card(id="a", status="in_transit", purchase_price="300"),
            card(id="b", status="in_transit", purchase_price="150"),
            card(id="c", status="in_hand", purchase_price="999"),
        ],
        today=TODAY,
    )
    assert r.in_transit_count == 2
    assert r.in_transit_dollars == Decimal("450.00")


# --------------------------------------------------------------------------
# Honesty rules
# --------------------------------------------------------------------------
def test_quiet_week_still_produces_a_report():
    r = reports.build_weekly([], today=TODAY)
    assert r.quiet
    text = reports.render_text(r)
    assert "No purchases and no sales" in text


def test_weekly_report_never_claims_a_capital_position():
    """The weekly has no capital section, so it must not imply one.

    The standing "NOT AVAILABLE" disclaimer was removed from the weekly on
    2026-09-30 (Brady's call) because it never changed and so taught the reader
    to skip the footer. The rule it protected still holds and is asserted here
    from the other direction: the weekly must not print a capital figure at all.
    `CAPITAL_UNAVAILABLE` survives for the MONTHLY report, which does have that
    section and must print it rather than substituting cost-of-inventory.
    """
    r = reports.build_weekly([card(purchase_price="5000")], today=TODAY)
    text = reports.render_text(r)
    for banned in ("Capital position", "allocation", "tranche", "cash available"):
        assert banned.lower() not in text.lower()
    # ...and the constant is still available for the monthly report to use.
    assert "capital_events" in reports.CAPITAL_UNAVAILABLE


def test_a_clean_week_has_no_gaps_block_at_all():
    r = reports.build_weekly([card()], today=TODAY)
    assert r.gaps == []
    assert "NOT COMPUTED" not in reports.render_text(r)


def test_missing_purchase_date_is_surfaced_not_swallowed():
    """A card with no purchase_date silently drops out of every aging bucket."""
    r = reports.build_weekly([card(id="x", purchase_date=None)], today=TODAY)
    assert r.source_counts["missing_purchase_date"] == 1
    assert any("purchase_date" in g for g in r.gaps)


def test_row_counts_travel_with_the_report():
    r = reports.build_weekly([card(id="a"), sold(id="b")], today=TODAY)
    assert r.source_counts["cards_scanned"] == 2
    assert r.source_counts["sold_all_time"] == 1
    assert r.source_counts["unsold"] == 1


# --------------------------------------------------------------------------
# Context numbers
# --------------------------------------------------------------------------
def test_best_and_worst_close_out():
    r = reports.build_weekly(
        [
            sold(id="win", sale_price="500"),
            sold(id="lose", sale_price="50"),
        ],
        today=TODAY,
    )
    assert r.best_sale.card_id == "win"
    assert r.worst_sale.card_id == "lose"


def test_trailing_average_uses_earlier_weeks_only():
    """This week's sale must not leak into its own comparison baseline."""
    this_week = sold(id="now", sale_date="2026-09-25")
    last_week = sold(id="prior", sale_date="2026-09-18")

    r = reports.build_weekly([this_week, last_week], today=TODAY)

    assert len(r.sales) == 1
    # one prior week had +62.00, three had nothing -> 62 / 4
    assert r.prior_4wk_average == Decimal("15.50")


def test_label_uses_player_not_player_name():
    """The column is `player`. `player_name` does not exist — a 9/17 trap."""
    line = reports.build_weekly(
        [card(player="Victor Wembanyama", purchase_date="2026-09-25")], today=TODAY
    ).purchases[0]
    assert "Wembanyama" in line.label
