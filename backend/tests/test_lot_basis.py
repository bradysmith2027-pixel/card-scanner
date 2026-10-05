"""Tests for splitting a lot's cost.

The main thing: the cards I entered plus the bulk remainder always add up to
what the lot cost, to the cent. No cost gets lost or made up.
"""

from decimal import Decimal

import pytest

from app.lot_basis import (
    LotBasisError,
    LotCard,
    allocate_lot_basis,
    lot_all_in_cost,
)

pytestmark = pytest.mark.unit


def lot(total="200.00", **kw):
    base = {"total_cost": total}
    base.update(kw)
    return base


def test_all_in_includes_shipping_tax_and_other():
    assert lot_all_in_cost(
        lot("200.00", shipping_in="10.00", purchase_tax="5.00", other_costs="2.50")
    ) == Decimal("217.50")


def test_pro_rata_not_even_split():
    """The basic case. A $150 card and a $50 card split $200 as 150/50."""
    r = allocate_lot_basis(
        lot("200.00"),
        [LotCard("hit", Decimal("150")), LotCard("mid", Decimal("50"))],
    )
    assert r.allocations == {"hit": Decimal("150.00"), "mid": Decimal("50.00")}
    assert r.bulk_basis == Decimal("0.00")


def test_the_yamal_scenario_bulk_remainder_protects_the_hit():
    """50 cards for $200, and I only enter the one good card.

    Without a bulk remainder, that card takes the whole $200 and looks like a
    loss. With the commons given a value, it only takes its fair share.
    """
    no_bulk = allocate_lot_basis(lot("200.00"), [LotCard("yamal", Decimal("120"))])
    assert no_bulk.allocations["yamal"] == Decimal("200.00")

    with_bulk = allocate_lot_basis(
        lot("200.00"),
        [LotCard("yamal", Decimal("120"))],
        bulk_remainder_value="83",  # 3 x $20 + 46 x $0.50
    )
    assert with_bulk.allocations["yamal"] == Decimal("118.23")
    assert with_bulk.bulk_basis == Decimal("81.77")
    assert with_bulk.allocations["yamal"] + with_bulk.bulk_basis == Decimal("200.00")


def test_everything_sums_to_all_in_including_bulk():
    for total in ("0.01", "37.00", "199.99", "5000.13"):
        for n in (1, 2, 3, 7):
            r = allocate_lot_basis(
                lot(total),
                [LotCard(f"c{i}", Decimal(i + 1)) for i in range(n)],
                bulk_remainder_value="13",
            )
            assert sum(r.allocations.values()) + r.bulk_basis == Decimal(total)


def test_pennies_reconcile_across_cards_and_bulk_together():
    """The leftover penny shouldn't always end up on the bulk.

    If the cards got split first and the bulk just got whatever was left, it
    would always lean the same way.
    """
    r = allocate_lot_basis(
        lot("100.00"),
        [LotCard("a", Decimal("1")), LotCard("b", Decimal("1"))],
        bulk_remainder_value="1",
    )
    assert sum(r.allocations.values()) + r.bulk_basis == Decimal("100.00")


def test_shipping_and_tax_are_allocated_too():
    """Shipping on the lot is part of the cost, so it has to get split onto the cards."""
    r = allocate_lot_basis(
        lot("100.00", shipping_in="20.00"), [LotCard("a", Decimal("1"))]
    )
    assert r.lot_all_in == Decimal("120.00")
    assert r.allocations["a"] == Decimal("120.00")


def test_no_values_falls_back_to_even_split_and_warns():
    r = allocate_lot_basis(lot("90.00"), [LotCard(x) for x in "abc"])
    assert r.even_split_fallback is True
    assert all(v == Decimal("30.00") for v in r.allocations.values())
    assert any("evenly" in w for w in r.warnings)


def test_even_split_fallback_ignores_bulk_rather_than_inventing_a_ratio():
    """With no card values there's nothing to compare the bulk to."""
    r = allocate_lot_basis(
        lot("90.00"), [LotCard(x) for x in "abc"], bulk_remainder_value="1000"
    )
    assert r.bulk_basis == Decimal("0.00")
    assert sum(r.allocations.values()) == Decimal("90.00")


def test_warns_when_most_of_the_cost_went_to_unentered_cards():
    r = allocate_lot_basis(
        lot("100.00"), [LotCard("a", Decimal("10"))], bulk_remainder_value="90"
    )
    assert any("did not enter individually" in w for w in r.warnings)


def test_no_cards_is_rejected():
    with pytest.raises(LotBasisError, match="nothing to allocate"):
        allocate_lot_basis(lot(), [])


def test_negative_bulk_is_rejected():
    with pytest.raises(LotBasisError, match="cannot be negative"):
        allocate_lot_basis(lot(), [LotCard("a", Decimal("1"))], bulk_remainder_value="-5")


def test_free_lot_allocates_zero_not_an_error():
    """A lot I traded for or got for free costs $0. That's fine, not an error."""
    r = allocate_lot_basis(lot("0"), [LotCard("a", Decimal("50"))])
    assert r.allocations["a"] == Decimal("0.00")
