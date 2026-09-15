"""Endpoint tests for POST /lots.

Reuses the multi-table fake from test_trades rather than keeping a second copy
in sync — the lesson from profit being implemented in six places.
"""

from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.test_trades import MultiDB, _Client

client = TestClient(app)


@pytest.fixture
def db():
    d = MultiDB()
    d.tables["purchase_lots"] = []
    factory = lambda token: _Client(d)  # noqa: E731
    with patch("app.routers.lots.user_client", factory), \
         patch("app.routers.cards.user_client", factory):
        yield d


def lot_card(**kw):
    base = {"player": "P", "year": "2024", "set_name": "Prizm", "category": "basketball"}
    base.update(kw)
    return base


def post(payload):
    return client.post("/lots", json=payload)


def test_lot_cost_is_allocated_pro_rata_onto_cards(auth, db):
    r = post({
        "purchase_date": "2026-09-14",
        "total_cost": "200.00",
        "cards": [lot_card(est_value="150"), lot_card(est_value="50")],
    })
    assert r.status_code == 201, r.text
    prices = [c["purchase_price"] for c in db.inserts["cards"]]
    assert prices == ["150.00", "50.00"]


def test_cards_are_linked_to_the_lot(auth, db):
    post({
        "purchase_date": "2026-09-14", "total_cost": "100.00",
        "cards": [lot_card(est_value="1")],
    })
    lot_id = db.inserts["purchase_lots"][0] and "purchase_lots-1"
    assert db.inserts["cards"][0]["lot_id"] == lot_id


def test_bulk_remainder_is_persisted_on_the_lot(auth, db):
    r = post({
        "purchase_date": "2026-09-14", "total_cost": "200.00",
        "cards": [lot_card(est_value="120")],
        "bulk_remainder_value": "83",
    })
    body = r.json()
    assert Decimal(body["bulk_basis"]) == Decimal("81.77")
    assert db.inserts["purchase_lots"][0]["bulk_basis"] == "81.77"
    # The entered card carries only its share, not the whole lot.
    assert db.inserts["cards"][0]["purchase_price"] == "118.23"


def test_lot_shipping_reaches_the_cards(auth, db):
    r = post({
        "purchase_date": "2026-09-14", "total_cost": "100.00",
        "shipping_in": "20.00",
        "cards": [lot_card(est_value="1")],
    })
    assert Decimal(r.json()["lot_all_in"]) == Decimal("120.00")
    assert db.inserts["cards"][0]["purchase_price"] == "120.00"


def test_per_card_shipping_is_cleared_to_avoid_double_counting(auth, db):
    """Lot-level costs are already inside the allocated basis."""
    post({
        "purchase_date": "2026-09-14", "total_cost": "100.00",
        "shipping_in": "20.00",
        "cards": [lot_card(est_value="1", shipping_in="9.99", purchase_tax="3.00")],
    })
    row = db.inserts["cards"][0]
    assert row["shipping_in"] is None
    assert row["purchase_tax"] is None


def test_client_supplied_purchase_price_is_overridden_by_allocation(auth, db):
    """The allocator decides basis. A stray price in the payload must not win."""
    post({
        "purchase_date": "2026-09-14", "total_cost": "60.00",
        "cards": [lot_card(est_value="1", purchase_price="999.00")],
    })
    assert db.inserts["cards"][0]["purchase_price"] == "60.00"


def test_missing_values_warn_about_even_split(auth, db):
    r = post({
        "purchase_date": "2026-09-14", "total_cost": "90.00",
        "cards": [lot_card(), lot_card(), lot_card()],
    })
    body = r.json()
    assert body["even_split_fallback"] is True
    assert any("evenly" in w for w in body["warnings"])


def test_warns_when_card_count_exceeds_entered_and_no_bulk_value(auth, db):
    r = post({
        "purchase_date": "2026-09-14", "total_cost": "200.00",
        "card_count": 50,
        "cards": [lot_card(est_value="120")],
    })
    assert any("other 49 were given no value" in w for w in r.json()["warnings"])


def test_no_cards_is_rejected(auth, db):
    r = post({
        "purchase_date": "2026-09-14", "total_cost": "100.00", "cards": [],
    })
    assert r.status_code == 422


def test_negative_total_cost_is_rejected(auth, db):
    r = post({
        "purchase_date": "2026-09-14", "total_cost": "-5.00",
        "cards": [lot_card(est_value="1")],
    })
    assert r.status_code == 422


def test_bad_source_is_rejected_at_the_api_boundary(auth, db):
    """Must 422 here, not reach Postgres and come back as an opaque 23514."""
    r = post({
        "purchase_date": "2026-09-14", "total_cost": "10.00",
        "source": "craigslist",
        "cards": [lot_card(est_value="1")],
    })
    assert r.status_code == 422


def test_lot_insert_failure_creates_no_cards(auth, db):
    db.fail_on = "purchase_lots"
    r = post({
        "purchase_date": "2026-09-14", "total_cost": "100.00",
        "cards": [lot_card(est_value="1")],
    })
    assert r.status_code == 502
    assert "cards" not in db.inserts


def test_position_type_defaults_to_flip_for_lot_cards(auth, db):
    post({
        "purchase_date": "2026-09-14", "total_cost": "100.00",
        "cards": [lot_card(est_value="1")],
    })
    assert db.inserts["cards"][0]["position_type"] == "flip"
