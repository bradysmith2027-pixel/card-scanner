"""Tests for lot cost allocation.

The guarantee: a lot's cost is fully distributed and never invented. Entered
cards + bulk remainder must equal the lot's all-in cost, to the penny.
"""

from decimal import Decimal

import pytest

from app.lot_basis import (
    LotBasisError,
    LotCard,
    allocate_lot_basis,
    lot_all_in_cost,
)


def lot(total="200.00", **kw):
    base = {"total_cost": total}
    base.update(kw)
    return base


def test_all_in_includes_shipping_tax_and_other():
    assert lot_all_in_cost(
        lot("200.00", shipping_in="10.00", purchase_tax="5.00", other_costs="2.50")
    ) == Decimal("217.50")


def test_pro_rata_not_even_split():
    """The whole point. A $150 card and a $50 card split $200 as 150/50."""
    r = allocate_lot_basis(
        lot("200.00"),
        [LotCard("hit", Decimal("150")), LotCard("mid", Decimal("50"))],
    )
    assert r.allocations == {"hit": Decimal("150.00"), "mid": Decimal("50.00")}
    assert r.bulk_basis == Decimal("0.00")


def test_the_yamal_scenario_bulk_remainder_protects_the_hit():
    """The case from the T1 plan: 50 cards, $200, only the hit entered.

    Without a bulk remainder the hit absorbs the entire $200 and looks like a
    loser. With the commons valued, it carries ~its fair share.
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
    """The leftover cent must not always land in bulk.

    Allocating cards first and giving bulk the remainder would drift the lot
    out of balance in one direction over time.
    """
    r = allocate_lot_basis(
        lot("100.00"),
        [LotCard("a", Decimal("1")), LotCard("b", Decimal("1"))],
        bulk_remainder_value="1",
    )
    assert sum(r.allocations.values()) + r.bulk_basis == Decimal("100.00")


def test_shipping_and_tax_are_allocated_too():
    """Lot shipping is part of basis — it must reach the cards."""
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
    """With no card values there is nothing to weigh bulk against."""
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
    """A traded-for or gifted lot has zero basis. That's valid, not a failure."""
    r = allocate_lot_basis(lot("0"), [LotCard("a", Decimal("50"))])
    assert r.allocations["a"] == Decimal("0.00")
