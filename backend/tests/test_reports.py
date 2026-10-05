"""test_reports.py

Tests for the Weekly Ops report.

The most important one is test_fee_gap_changes_the_report. A sale with a
missing fee has to give a different report than one with the fee. My profit bug
lasted months because the old test checked 250 - 100 == 150, which passes with
or without fees since none of the test data had fees. Don't want that again.
"""

from datetime import date
from decimal import Decimal

import pytest

from app import reports

pytestmark = pytest.mark.unit

TODAY = date(2026, 9, 28)


def card(**over):
    """A normal in-hand card. Each test only changes what it's testing."""
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
    """The same card I checked against the real database.

    $100 card + $13 other costs = $113 all in. Sold for $200, $26 fees, $5 to
    ship, $6 shipping paid by the buyer -> $175 in -> +$62.00 profit.
    The old code said +$100.00 for this card.
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
# Report dates
# --------------------------------------------------------------------------
def test_window_is_half_open_so_nothing_is_double_counted():
    start, end = reports.week_bounds(TODAY)
    assert start == date(2026, 9, 21)
    assert end == TODAY

    # start counts, so a sale on the first day is in this report.
    r = reports.build_weekly([sold(sale_date="2026-09-21")], today=TODAY)
    assert len(r.sales) == 1

    # the day before start goes in last week's report.
    r = reports.build_weekly([sold(sale_date="2026-09-20")], today=TODAY)
    assert r.sales == []

    # end doesn't count, so a sale later today goes in next week's report.
    r = reports.build_weekly([sold(sale_date="2026-09-28")], today=TODAY)
    assert r.sales == []

    # and it does show up in next week's report, so nothing gets missed.
    r = reports.build_weekly(
        [sold(sale_date="2026-09-28")], today=date(2026, 10, 5)
    )
    assert len(r.sales) == 1


# --------------------------------------------------------------------------
# The report doesn't do its own profit math
# --------------------------------------------------------------------------
def test_realized_profit_is_net_not_gross():
    """$100 all in, $200 sale, $26 fees, $5 to ship, $6 collected -> +$62.

    If this ever says 100.00, the report is doing profit without fees again,
    which is the bug that gave me wrong numbers for months.
    """
    r = reports.build_weekly([sold()], today=TODAY)
    assert r.realized_profit == Decimal("62.00")


# --------------------------------------------------------------------------
# Make sure a missing fee actually changes the report
# --------------------------------------------------------------------------
def test_fee_gap_changes_the_report():
    """A missing fee should actually change the report, not just the wording."""
    with_fee = reports.build_weekly(
        [sold(sale_channel="ebay", platform_fees="26")], today=TODAY
    )
    without_fee = reports.build_weekly(
        [sold(sale_channel="ebay", platform_fees="0")], today=TODAY
    )

    assert with_fee.fee_gaps == []
    assert len(without_fee.fee_gaps) == 1
    # and the money changes too, not just the warnings
    assert without_fee.realized_profit != with_fee.realized_profit
    assert reports.render_text(without_fee) != reports.render_text(with_fee)
    assert "FEES MISSING" in reports.render_text(without_fee)
    assert "FEES MISSING" not in reports.render_text(with_fee)


def test_zero_fee_on_a_cash_channel_is_not_a_gap():
    """From when I imported my spreadsheet.

    22 of 23 sales with a $0 fee were fine, because cash and in person sales
    don't have fees. If every $0 got flagged I'd just start ignoring the report.
    """
    for channel in ("show", "discord", "facebook", "instagram"):
        r = reports.build_weekly(
            [sold(sale_channel=channel, platform_fees="0")], today=TODAY
        )
        assert r.fee_gaps == [], f"{channel} should not be flagged"


def test_missing_channel_is_not_flagged():
    """No sale channel entered doesn't mean the fee is missing."""
    r = reports.build_weekly([sold(sale_channel=None, platform_fees="0")], today=TODAY)
    assert r.fee_gaps == []


# --------------------------------------------------------------------------
# Aging
# --------------------------------------------------------------------------
def test_aging_reports_only_the_week_a_threshold_is_crossed():
    """A 200 day old card shouldn't show up in the 90 day list every single week."""
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
# Money tied up in transit or at grading
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
# Don't show numbers that can't be calculated
# --------------------------------------------------------------------------
def test_quiet_week_still_produces_a_report():
    r = reports.build_weekly([], today=TODAY)
    assert r.quiet
    text = reports.render_text(r)
    assert "No purchases and no sales" in text


def test_weekly_report_never_claims_a_capital_position():
    """The weekly doesn't have a capital section, so it shouldn't show a capital number.

    I took the "NOT AVAILABLE" note off the weekly because it said the same thing
    every week and I'd stop reading it. But the weekly still shouldn't show any
    capital number. CAPITAL_UNAVAILABLE is still there for the monthly report,
    which does have a capital section.
    """
    r = reports.build_weekly([card(purchase_price="5000")], today=TODAY)
    text = reports.render_text(r)
    for banned in ("Capital position", "allocation", "tranche", "cash available"):
        assert banned.lower() not in text.lower()
    # ...and the monthly report can still use it.
    assert "capital_events" in reports.CAPITAL_UNAVAILABLE


def test_a_clean_week_has_no_gaps_block_at_all():
    r = reports.build_weekly([card()], today=TODAY)
    assert r.gaps == []
    assert "NOT COMPUTED" not in reports.render_text(r)


def test_missing_purchase_date_is_surfaced_not_swallowed():
    """A card with no purchase_date doesn't show up in any of the aging numbers."""
    r = reports.build_weekly([card(id="x", purchase_date=None)], today=TODAY)
    assert r.source_counts["missing_purchase_date"] == 1
    assert any("purchase_date" in g for g in r.gaps)


def test_row_counts_travel_with_the_report():
    r = reports.build_weekly([card(id="a"), sold(id="b")], today=TODAY)
    assert r.source_counts["cards_scanned"] == 2
    assert r.source_counts["sold_all_time"] == 1
    assert r.source_counts["unsold"] == 1


# --------------------------------------------------------------------------
# Comparison numbers
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
    """This week's sale shouldn't count in the average it gets compared to."""
    this_week = sold(id="now", sale_date="2026-09-25")
    last_week = sold(id="prior", sale_date="2026-09-18")

    r = reports.build_weekly([this_week, last_week], today=TODAY)

    assert len(r.sales) == 1
    # one week before had +62.00, three had nothing -> 62 / 4
    assert r.prior_4wk_average == Decimal("15.50")


def test_label_uses_player_not_player_name():
    """The column is player. There's no player_name, that got me during the import."""
    line = reports.build_weekly(
        [card(player="Victor Wembanyama", purchase_date="2026-09-25")], today=TODAY
    ).purchases[0]
    assert "Wembanyama" in line.label
