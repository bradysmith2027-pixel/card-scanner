"""
test_cards_logic.py

Tests for adding a card (POST /cards) with a fake database (see
conftest.fake_db). They check:
  - user_id comes from the server, so nobody can add a card as someone else
  - category is required, and negative money gets a 422 before the database
  - status defaults to in_hand, and card_type works
  - if the database sends back nothing, it's a 502 and not a fake success
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app

pytestmark = pytest.mark.unit

client = TestClient(app)

_BODY = {"player": "X", "year": "2024", "set_name": "Topps", "category": "basketball"}


def test_user_id_is_stamped_server_side(auth, fake_db):
    # Request tries to use someone else's id. The server should ignore it and use the real one.
    resp = client.post("/cards", json={**_BODY, "user_id": "attacker-id"})
    assert resp.status_code == 201
    assert fake_db.inserted["user_id"] == auth          # set by the server
    assert resp.json()["user_id"] == auth
    assert fake_db.inserted["user_id"] != "attacker-id"  # fake id ignored


def test_status_defaults_to_in_hand(auth, fake_db):
    resp = client.post("/cards", json=_BODY)
    assert resp.status_code == 201
    assert fake_db.inserted["status"] == "in_hand"


def test_category_is_required(auth, fake_db):
    body = {k: v for k, v in _BODY.items() if k != "category"}
    resp = client.post("/cards", json=body)
    assert resp.status_code == 422  # rejected before it hits the database


def test_negative_price_rejected(auth, fake_db):
    resp = client.post("/cards", json={**_BODY, "purchase_price": "-5"})
    assert resp.status_code == 422


def test_absurd_price_rejected(auth, fake_db):
    resp = client.post("/cards", json={**_BODY, "purchase_price": "99999999"})
    assert resp.status_code == 422  # over the $10M limit


def test_card_type_is_accepted(auth, fake_db):
    resp = client.post("/cards", json={**_BODY, "card_type": "Blue Refractor"})
    assert resp.status_code == 201
    assert fake_db.inserted["card_type"] == "Blue Refractor"


def test_empty_db_response_is_502(auth, fake_db):
    fake_db.insert_result = []  # database sent back nothing
    resp = client.post("/cards", json=_BODY)
    assert resp.status_code == 502
