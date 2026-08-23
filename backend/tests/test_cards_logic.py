"""
test_cards_logic.py — mocked unit tests for POST /cards business logic.

No DB: supabase user_client is faked (see conftest.fake_db). These prove the
security- and correctness-critical behavior of card creation without spend:
  - user_id is stamped server-side (a client can't forge ownership)
  - category is required; negative money is rejected (422 before any DB call)
  - status defaults to in_hand; card_type is accepted
  - an empty DB response surfaces as 502, not a silent success
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app

pytestmark = pytest.mark.unit

client = TestClient(app)

_BODY = {"player": "X", "year": "2024", "set_name": "Topps", "category": "basketball"}


def test_user_id_is_stamped_server_side(auth, fake_db):
    # Client tries to claim someone else's id; server must ignore + stamp its own.
    resp = client.post("/cards", json={**_BODY, "user_id": "attacker-id"})
    assert resp.status_code == 201
    assert fake_db.inserted["user_id"] == auth          # server-stamped
    assert resp.json()["user_id"] == auth
    assert fake_db.inserted["user_id"] != "attacker-id"  # forged id ignored


def test_status_defaults_to_in_hand(auth, fake_db):
    resp = client.post("/cards", json=_BODY)
    assert resp.status_code == 201
    assert fake_db.inserted["status"] == "in_hand"


def test_category_is_required(auth, fake_db):
    body = {k: v for k, v in _BODY.items() if k != "category"}
    resp = client.post("/cards", json=body)
    assert resp.status_code == 422  # rejected by validation, no DB call


def test_negative_price_rejected(auth, fake_db):
    resp = client.post("/cards", json={**_BODY, "purchase_price": "-5"})
    assert resp.status_code == 422


def test_absurd_price_rejected(auth, fake_db):
    resp = client.post("/cards", json={**_BODY, "purchase_price": "99999999"})
    assert resp.status_code == 422  # over the 10M sanity cap


def test_card_type_is_accepted(auth, fake_db):
    resp = client.post("/cards", json={**_BODY, "card_type": "Blue Refractor"})
    assert resp.status_code == 201
    assert fake_db.inserted["card_type"] == "Blue Refractor"


def test_empty_db_response_is_502(auth, fake_db):
    fake_db.insert_result = []  # DB returned no row
    resp = client.post("/cards", json=_BODY)
    assert resp.status_code == 502
