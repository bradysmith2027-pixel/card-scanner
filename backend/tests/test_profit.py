"""
test_profit.py

Tests for the profit math, plus two rules that protect it.

The app was showing profit about 2.5x too high for months and nothing caught
it, because there were no tests on the math. So now there are.

No database or network needed. app.profit just takes plain dicts.
"""

from datetime import date
from decimal import Decimal

import pytest

from app import profit
from app.routers.cards import CardClose, CardCreate, CardUpdate

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# The main example
# --------------------------------------------------------------------------
def test_spec_example_100_to_200_is_39_50_not_100():
    """Bought for $100, sold for $200. The old code said +$100, it's really $39.50.

    This is the exact case that made me write profit.py. If this ever returns
    100.00 again, the bug is back.
    """
    card = {
        "id": "c1",
        "status": "sold",
        "purchase_price": "100.00",
        "shipping_in": "5.00",
        "sale_price": "200.00",
        "platform_fees": "26.50",
        "shipping_out": "5.00",
    }
    grading = [{"cost": "24.00"}]

    assert profit.all_in_cost(card, profit.grading_cost_for(grading)) == Decimal("129.00")
    assert profit.net_proceeds(card) == Decimal("168.50")
    assert profit.net_profit(card, profit.grading_cost_for(grading)) == Decimal("39.50")

    gross = Decimal(card["sale_price"]) - Decimal(card["purchase_price"])
    assert gross == Decimal("100.00")  # what the old code said
    assert profit.net_profit(card, profit.grading_cost_for(grading)) < gross


# --------------------------------------------------------------------------
# Rule 1: unsold is None, not 0
# --------------------------------------------------------------------------
@pytest.mark.parametrize("status", ["in_hand", "in_transit", "at_grading"])
def test_unsold_card_has_none_profit_not_zero(status):
    """A 0 would get averaged into ROI and drag it down. None gets left out."""
    card = {"id": "c", "status": status, "purchase_price": "50", "sale_price": "80"}
    assert profit.net_profit(card) is None
    assert profit.net_proceeds(card) is None
    assert profit.roi(card) is None


def test_traded_away_books_no_revenue():
    """A trade isn't a sale, so there's no profit.

    I logged my Cam Ward ($1,039) and Bo Nix ($595) trades as break-even sales
    in my spreadsheet, and it pulled my ROI on big cards from 5.7% to 4.1%.
    traded_away should never count as money in.
    """
    card = {"id": "c", "status": "traded_away", "purchase_price": "500", "sale_price": "1039"}
    assert profit.net_profit(card) is None
    assert profit.net_proceeds(card) is None


def test_sale_price_alone_does_not_mean_sold():
    """Only the status decides if a card is sold."""
    card = {"id": "c", "status": "in_hand", "sale_price": "300", "purchase_price": "10"}
    assert profit.is_sold(card) is False
    assert profit.net_profit(card) is None


# --------------------------------------------------------------------------
# Rule 2: no cost means no ROI (not a crash, and not 0)
# --------------------------------------------------------------------------
def test_zero_basis_roi_is_none_not_division_error():
    """A card I pulled cost me nothing, so there's no ROI."""
    card = {"id": "c", "status": "sold", "purchase_price": "0", "sale_price": "40"}
    assert profit.all_in_cost(card) == Decimal("0.00")
    assert profit.net_profit(card) == Decimal("40.00")
    assert profit.roi(card) is None


def test_roi_is_a_ratio():
    card = {"id": "c", "status": "sold", "purchase_price": "100", "sale_price": "150"}
    assert profit.roi(card) == Decimal("0.500000")


# --------------------------------------------------------------------------
# shipping_collected counts as money in
# --------------------------------------------------------------------------
def test_shipping_collected_is_added_not_subtracted():
    """Shipping the buyer pays me counts as money in.

    Leaving it out made my eBay sales look worse than they really were.
    """
    card = {
        "id": "c", "status": "sold",
        "purchase_price": "10", "sale_price": "20",
        "shipping_collected": "6", "shipping_out": "5",
    }
    assert profit.net_proceeds(card) == Decimal("21.00")  # 20 + 6 - 5
    assert profit.net_profit(card) == Decimal("11.00")


# --------------------------------------------------------------------------
# Grading cost
# --------------------------------------------------------------------------
def test_grading_cost_is_summed_across_submissions():
    """A card can get sent in more than once (resubmits, crossovers)."""
    assert profit.grading_cost_for([{"cost": "20"}, {"cost": "18.50"}]) == Decimal("38.50")
    assert profit.grading_cost_for([]) == Decimal(0)
    assert profit.grading_cost_for(None) == Decimal(0)


def test_grading_cost_changes_the_answer():
    card = {"id": "c", "status": "sold", "purchase_price": "100", "sale_price": "160"}
    without = profit.net_profit(card)
    with_grading = profit.net_profit(card, profit.grading_cost_for([{"cost": "25"}]))
    assert without == Decimal("60.00")
    assert with_grading == Decimal("35.00")


# --------------------------------------------------------------------------
# hold_days: sold, unsold, and no purchase date
# --------------------------------------------------------------------------
def test_hold_days_stops_at_sale_date_when_sold():
    card = {
        "id": "c", "status": "sold",
        "purchase_date": "2026-06-01", "sale_date": "2026-08-01",
    }
    # Today doesn't matter for a sold card, the count stopped when it sold.
    assert profit.hold_days(card, date(2030, 1, 1)) == 61


def test_hold_days_runs_to_today_when_unsold():
    card = {"id": "c", "status": "in_hand", "purchase_date": "2026-08-01"}
    assert profit.hold_days(card, date(2026, 9, 13)) == 43


def test_missing_purchase_date_gives_none_hold_days():
    """A card with no purchase date just doesn't show up in any aging numbers,
    which is why the add card form requires the date."""
    assert profit.hold_days({"id": "c", "status": "in_hand"}) is None


# --------------------------------------------------------------------------
# Supabase sends numbers back as strings or floats, so check both
# --------------------------------------------------------------------------
@pytest.mark.parametrize("value", ["100.00", 100, 100.0, Decimal("100")])
def test_money_coercion_accepts_db_shapes(value):
    card = {"id": "c", "status": "sold", "purchase_price": value, "sale_price": "100"}
    assert profit.all_in_cost(card) == Decimal("100.00")


def test_null_costs_are_zero_not_poison():
    """One empty field shouldn't make the whole all_in_cost come out empty."""
    card = {"id": "c", "status": "sold", "purchase_price": "50",
            "shipping_in": None, "purchase_tax": None, "other_costs": None,
            "sale_price": "60", "platform_fees": None, "shipping_out": None}
    assert profit.all_in_cost(card) == Decimal("50.00")
    assert profit.net_profit(card) == Decimal("10.00")


# --------------------------------------------------------------------------
# enrich / enrich_many
# --------------------------------------------------------------------------
def test_enrich_adds_computed_fields_without_dropping_originals():
    card = {"id": "c", "status": "sold", "player": "Ohtani",
            "purchase_price": "10", "sale_price": "30"}
    out = profit.enrich(card)
    assert out["player"] == "Ohtani"  # original fields are still there
    for key in ("all_in_cost", "net_proceeds", "net_profit", "roi",
                "hold_days", "grading_cost"):
        assert key in out


def test_enrich_many_maps_grading_costs_by_card_id():
    cards = [
        {"id": "a", "status": "sold", "purchase_price": "10", "sale_price": "100"},
        {"id": "b", "status": "sold", "purchase_price": "10", "sale_price": "100"},
    ]
    out = profit.enrich_many(cards, {"a": [{"cost": "50"}]})
    by_id = {c["id"]: c for c in out}
    assert by_id["a"]["net_profit"] == Decimal("40.00")   # grading taken out
    assert by_id["b"]["net_profit"] == Decimal("90.00")   # b wasn't graded


# --------------------------------------------------------------------------
# Rule: position_type can't be changed
# --------------------------------------------------------------------------
def test_position_type_is_not_patchable():
    """position_type can't be in CardUpdate.

    Any field in CardUpdate can be edited, so leaving it out is what stops it.
    If it got added, I could move flips that didn't sell into my collection and
    my ROI would look better than it really is.
    """
    assert "position_type" not in CardUpdate.model_fields


def test_position_type_is_settable_at_creation_and_defaults_to_flip():
    assert "position_type" in CardCreate.model_fields
    assert CardCreate(player="p", year="2026", set_name="s",
                      category="football").position_type == "flip"


def test_ownership_fields_still_not_patchable():
    """Make sure this older rule still holds."""
    for field in ("id", "user_id", "created_at", "updated_at"):
        assert field not in CardUpdate.model_fields


@pytest.mark.parametrize("bad", ["Flip", "HOLD", "collection", ""])
def test_invalid_position_type_rejected(bad):
    """A bad value gets a clear 422 instead of a confusing Postgres error
    (23514). That's what broke saving cards back in August."""
    with pytest.raises(Exception):
        CardCreate(player="p", year="2026", set_name="s",
                   category="football", position_type=bad)


# --------------------------------------------------------------------------
# The serial (migration 010)
#
# The opposite of position_type. This one can be edited, because a serial that
# got read wrong is just a typo.
# --------------------------------------------------------------------------
def test_serial_is_patchable_and_settable():
    assert "serial" in CardCreate.model_fields
    assert "serial" in CardUpdate.model_fields


def test_serial_defaults_to_none_meaning_not_numbered():
    """Empty means the card isn't numbered, not that something is missing."""
    card = CardCreate(player="p", year="2026", set_name="s", category="football")
    assert card.serial is None


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_blank_serial_becomes_none_not_empty_string(blank):
    """The serial column doesn't allow ''. An empty box in the browser sends ""
    instead of null, so without this, leaving the serial blank would make the
    save fail.
    """
    assert CardCreate(player="p", year="2026", set_name="s",
                      category="football", serial=blank).serial is None
    assert CardUpdate(serial=blank).serial is None


def test_serial_is_trimmed_so_one_print_run_is_one_value():
    """' 9/25' and '9/25' should be the same serial. The database doesn't allow
    extra spaces anyway."""
    assert CardCreate(player="p", year="2026", set_name="s",
                      category="football", serial="  9/25  ").serial == "9/25"


@pytest.mark.parametrize("serial", ["9/25", "1/1", "FOTL 12/99", "A/50", "/25"])
def test_real_world_serial_formats_are_accepted(serial):
    """Why the serial is text and not two numbers (see migration 010). These are
    all real serials and none of them fit number/number."""
    assert CardCreate(player="p", year="2026", set_name="s",
                      category="football", serial=serial).serial == serial


def test_overlong_serial_rejected_at_the_api_not_by_postgres():
    """Same 32 character limit as migration 010, so a bad OCR read gets a clear
    422."""
    with pytest.raises(Exception):
        CardCreate(player="p", year="2026", set_name="s",
                   category="football", serial="x" * 33)


def test_clearing_a_serial_is_expressible_on_patch():
    """I need to be able to clear a serial I entered by mistake. Leaving the
    field out doesn't change anything, so sending "" is how to clear it."""
    assert CardUpdate(serial="").model_dump(exclude_unset=True) == {"serial": None}


# --------------------------------------------------------------------------
# Rule: marking a card sold requires fees
# --------------------------------------------------------------------------
def test_close_out_requires_fees():
    """If I don't enter fees when I log the sale I never will, and profit would
    be too high again."""
    with pytest.raises(Exception):
        CardClose(sale_price=Decimal("100"), sale_date=date(2026, 9, 13))


def test_close_out_accepts_explicit_zero_fees():
    """Some sales have no fees (Discord, cash at a show), so 0 has to work."""
    c = CardClose(sale_price=Decimal("100"), sale_date=date(2026, 9, 13),
                  platform_fees=Decimal("0"), shipping_out=Decimal("0"))
    assert c.platform_fees == Decimal("0")
    assert c.shipping_collected == Decimal("0")


def test_close_out_rejects_negative_money():
    with pytest.raises(Exception):
        CardClose(sale_price=Decimal("100"), sale_date=date(2026, 9, 13),
                  platform_fees=Decimal("-5"), shipping_out=Decimal("0"))
