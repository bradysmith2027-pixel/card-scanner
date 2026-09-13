"""
test_profit.py — the profit math, and the two contracts that protect it.

These tests exist because the app overstated profit by ~2.5x for months and
nothing caught it. The math had no coverage at all, so there was nothing to
fail when four different files each computed `sale_price - purchase_price`.

Pure unit tests: no DB, no network. `app.profit` takes plain dicts by design.
"""

from datetime import date
from decimal import Decimal

import pytest

from app import profit
from app.routers.cards import CardClose, CardCreate, CardUpdate


# --------------------------------------------------------------------------
# The headline case from the build spec
# --------------------------------------------------------------------------
def test_spec_example_100_to_200_is_39_50_not_100():
    """$100 -> $200 reported +$100. The truth is $39.50.

    This exact scenario is the reason profit.py exists, so it gets a test by
    name. If this ever returns 100.00 again, the gross-profit bug is back.
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
# Rule 1 — unsold is None, never zero
# --------------------------------------------------------------------------
@pytest.mark.parametrize("status", ["in_hand", "in_transit", "at_grading"])
def test_unsold_card_has_none_profit_not_zero(status):
    """0 averages into ROI and drags it toward nothing. None is excluded."""
    card = {"id": "c", "status": status, "purchase_price": "50", "sale_price": "80"}
    assert profit.net_profit(card) is None
    assert profit.net_proceeds(card) is None
    assert profit.roi(card) is None


def test_traded_away_books_no_revenue():
    """A trade is NOT a sale — carry-over basis, no realized profit.

    Cam Ward ($1,039) and Bo Nix ($595) were logged as break-even sales, which
    dragged high-value ROI from 5.7% down to 4.1%. `traded_away` must never
    produce revenue.
    """
    card = {"id": "c", "status": "traded_away", "purchase_price": "500", "sale_price": "1039"}
    assert profit.net_profit(card) is None
    assert profit.net_proceeds(card) is None


def test_sale_price_alone_does_not_mean_sold():
    """Status is the only thing that marks a sale."""
    card = {"id": "c", "status": "in_hand", "sale_price": "300", "purchase_price": "10"}
    assert profit.is_sold(card) is False
    assert profit.net_profit(card) is None


# --------------------------------------------------------------------------
# Rule 2 — zero basis means undefined ROI, not a crash and not zero
# --------------------------------------------------------------------------
def test_zero_basis_roi_is_none_not_division_error():
    """A pulled card has no cost basis, so ROI is undefined."""
    card = {"id": "c", "status": "sold", "purchase_price": "0", "sale_price": "40"}
    assert profit.all_in_cost(card) == Decimal("0.00")
    assert profit.net_profit(card) == Decimal("40.00")
    assert profit.roi(card) is None


def test_roi_is_a_ratio():
    card = {"id": "c", "status": "sold", "purchase_price": "100", "sale_price": "150"}
    assert profit.roi(card) == Decimal("0.500000")


# --------------------------------------------------------------------------
# shipping_collected is REVENUE
# --------------------------------------------------------------------------
def test_shipping_collected_is_added_not_subtracted():
    """The customer-paid shipping fee is revenue.

    Omitting it was the mirror image of the gross-profit bug: it made eBay
    sales look worse than they actually were.
    """
    card = {
        "id": "c", "status": "sold",
        "purchase_price": "10", "sale_price": "20",
        "shipping_collected": "6", "shipping_out": "5",
    }
    assert profit.net_proceeds(card) == Decimal("21.00")  # 20 + 6 - 5
    assert profit.net_profit(card) == Decimal("11.00")


# --------------------------------------------------------------------------
# The grading join everyone forgot
# --------------------------------------------------------------------------
def test_grading_cost_is_summed_across_submissions():
    """Resubmits and crossovers mean a card can have several submissions."""
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
# hold_days — both branches, plus the silent hole
# --------------------------------------------------------------------------
def test_hold_days_stops_at_sale_date_when_sold():
    card = {
        "id": "c", "status": "sold",
        "purchase_date": "2026-06-01", "sale_date": "2026-08-01",
    }
    # Today is irrelevant for a sold card — the clock stopped.
    assert profit.hold_days(card, date(2030, 1, 1)) == 61


def test_hold_days_runs_to_today_when_unsold():
    card = {"id": "c", "status": "in_hand", "purchase_date": "2026-08-01"}
    assert profit.hold_days(card, date(2026, 9, 13)) == 43


def test_missing_purchase_date_gives_none_hold_days():
    """This is a real reporting hole: such a card vanishes from every aging
    bucket silently. Manual entry must require purchase_date."""
    assert profit.hold_days({"id": "c", "status": "in_hand"}) is None


# --------------------------------------------------------------------------
# Coercion — Supabase returns numerics as str or float depending on client
# --------------------------------------------------------------------------
@pytest.mark.parametrize("value", ["100.00", 100, 100.0, Decimal("100")])
def test_money_coercion_accepts_db_shapes(value):
    card = {"id": "c", "status": "sold", "purchase_price": value, "sale_price": "100"}
    assert profit.all_in_cost(card) == Decimal("100.00")


def test_null_costs_are_zero_not_poison():
    """NULL must not propagate through a SUM and wipe out all_in_cost."""
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
    assert out["player"] == "Ohtani"  # original keys survive
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
    assert by_id["a"]["net_profit"] == Decimal("40.00")   # grading applied
    assert by_id["b"]["net_profit"] == Decimal("90.00")   # none for b


# --------------------------------------------------------------------------
# CONTRACT 1 — position_type is immutable
# --------------------------------------------------------------------------
def test_position_type_is_not_patchable():
    """🔒 The no-reclassification rule, enforced structurally.

    `update_card` uses `exclude_unset`, so ANY field on CardUpdate is
    writable. Omission IS the enforcement. If someone adds position_type to
    CardUpdate for convenience, failed flips can be laundered into the
    'personal collection' bucket — they leave the performance report, ROI
    looks clean, and capital sits frozen while the review shows profit.
    """
    assert "position_type" not in CardUpdate.model_fields


def test_position_type_is_settable_at_creation_and_defaults_to_flip():
    assert "position_type" in CardCreate.model_fields
    assert CardCreate(player="p", year="2026", set_name="s",
                      category="football").position_type == "flip"


def test_ownership_fields_still_not_patchable():
    """Regression guard on the pre-existing rule."""
    for field in ("id", "user_id", "created_at", "updated_at"):
        assert field not in CardUpdate.model_fields


@pytest.mark.parametrize("bad", ["Flip", "HOLD", "collection", ""])
def test_invalid_position_type_rejected(bad):
    """Literal validation gives a clean 422 instead of a Postgres 23514 —
    the failure mode behind the 2026-08-18 outage."""
    with pytest.raises(Exception):
        CardCreate(player="p", year="2026", set_name="s",
                   category="football", position_type=bad)


# --------------------------------------------------------------------------
# CONTRACT 2 — close-out requires fees
# --------------------------------------------------------------------------
def test_close_out_requires_fees():
    """Fees not captured at close-out are never captured. Optional fields get
    left blank, and profit quietly reverts to gross."""
    with pytest.raises(Exception):
        CardClose(sale_price=Decimal("100"), sale_date=date(2026, 9, 13))


def test_close_out_accepts_explicit_zero_fees():
    """Selling with no fees (Discord, cash at a show) is a statement, not an
    omission — so 0 must be expressible."""
    c = CardClose(sale_price=Decimal("100"), sale_date=date(2026, 9, 13),
                  platform_fees=Decimal("0"), shipping_out=Decimal("0"))
    assert c.platform_fees == Decimal("0")
    assert c.shipping_collected == Decimal("0")


def test_close_out_rejects_negative_money():
    with pytest.raises(Exception):
        CardClose(sale_price=Decimal("100"), sale_date=date(2026, 9, 13),
                  platform_fees=Decimal("-5"), shipping_out=Decimal("0"))
