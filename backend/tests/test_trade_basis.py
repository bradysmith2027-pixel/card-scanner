"""Tests for moving cost from traded cards to the cards I get back.

A trade should never create or lose cost. Whatever I gave up, plus any cash,
ends up on the cards I got, to the cent.
"""

from decimal import Decimal

import pytest

from app.trade_basis import (
    ReceivedCard,
    TradeBasisError,
    allocate_trade_basis,
)

pytestmark = pytest.mark.unit


def test_one_for_one_carries_basis_exactly():
    r = allocate_trade_basis(["100.00"], [ReceivedCard("B", Decimal("250"))])
    assert r.total_basis == Decimal("100.00")
    assert r.allocations == {"B": Decimal("100.00")}
    assert r.realized_gain == Decimal("0.00")


def test_trade_realizes_nothing_regardless_of_market_value():
    """The card I got is worth 4x the one I gave. The cost still just moves over.

    A trade isn't a profit. If this ever shows a gain, the app is counting a
    trade as a sale again.
    """
    r = allocate_trade_basis(["50.00"], [ReceivedCard("Big", Decimal("200"))])
    assert r.total_basis == Decimal("50.00")
    assert r.realized_gain == Decimal("0.00")


def test_cash_paid_increases_basis():
    r = allocate_trade_basis(["100.00"], [ReceivedCard("B", 1)], cash_boot="75.00")
    assert r.total_basis == Decimal("175.00")


def test_cash_received_decreases_basis():
    r = allocate_trade_basis(["100.00"], [ReceivedCard("B", 1)], cash_boot="-40.00")
    assert r.total_basis == Decimal("60.00")


def test_boot_exceeding_basis_floors_at_zero_and_reports_gain():
    """Got $100 cash back for a card that cost me $30.

    Cost can't go below 0. The extra $70 is real profit, so it has to show up
    instead of just disappearing.
    """
    r = allocate_trade_basis(["30.00"], [ReceivedCard("B", 1)], cash_boot="-100.00")
    assert r.total_basis == Decimal("0.00")
    assert r.realized_gain == Decimal("70.00")


def test_multi_card_allocates_pro_rata_by_value():
    """$300 of cost across cards worth 300 / 100 -> 75% / 25%."""
    r = allocate_trade_basis(
        ["300.00"],
        [ReceivedCard("X", Decimal("300")), ReceivedCard("Y", Decimal("100"))],
    )
    assert r.allocations["X"] == Decimal("225.00")
    assert r.allocations["Y"] == Decimal("75.00")
    assert not r.even_split_fallback


def test_many_to_many_sums_outgoing_basis():
    r = allocate_trade_basis(
        ["100.00", "50.00", "25.50"],
        [ReceivedCard("X", Decimal("1")), ReceivedCard("Y", Decimal("1"))],
    )
    assert r.total_basis == Decimal("175.50")


def test_pennies_reconcile_exactly_on_three_way_split():
    """$100.00 three ways is the classic lost penny case.

    Normal rounding gives 33.33 x3 = 99.99 and a cent goes missing. This makes
    sure it gets added back.
    """
    r = allocate_trade_basis(
        ["100.00"],
        [ReceivedCard(ref, Decimal("1")) for ref in ("A", "B", "C")],
    )
    assert sum(r.allocations.values()) == Decimal("100.00")
    assert sorted(r.allocations.values()) == [
        Decimal("33.33"),
        Decimal("33.33"),
        Decimal("33.34"),
    ]


def test_allocation_always_sums_to_total_across_awkward_splits():
    for total in ("0.01", "10.00", "99.99", "1234.57"):
        for n in (1, 2, 3, 7, 11):
            r = allocate_trade_basis(
                [total],
                [ReceivedCard(f"c{i}", Decimal("1")) for i in range(n)],
            )
            assert sum(r.allocations.values()) == Decimal(total)


def test_missing_values_fall_back_to_even_split_but_flag_it():
    """An even split is fine, but it has to be flagged so the app can warn me."""
    r = allocate_trade_basis(
        ["100.00"], [ReceivedCard("A"), ReceivedCard("B")]
    )
    assert r.even_split_fallback is True
    assert r.allocations == {"A": Decimal("50.00"), "B": Decimal("50.00")}


def test_no_cards_received_is_a_sale_not_a_trade():
    with pytest.raises(TradeBasisError, match="SALE"):
        allocate_trade_basis(["100.00"], [])


def test_no_cards_given_is_a_purchase_not_a_trade():
    with pytest.raises(TradeBasisError, match="PURCHASE"):
        allocate_trade_basis([], [ReceivedCard("B", 1)])


def test_pulled_card_with_zero_basis_carries_zero():
    """A card I pulled cost nothing, so trading it gives the new card no cost."""
    r = allocate_trade_basis(["0"], [ReceivedCard("B", Decimal("500"))])
    assert r.total_basis == Decimal("0.00")
    assert r.allocations["B"] == Decimal("0.00")
