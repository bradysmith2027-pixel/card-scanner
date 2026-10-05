"""
test_cards.py

Endpoint tests that don't need the real database or a real login.

These check that you get a 401 without logging in. That happens before any
Supabase call, so it works offline. Testing RLS with real rows needs a real
token, which is what the live_ scripts are for.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app

pytestmark = pytest.mark.unit

client = TestClient(app)


def test_health_ok():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_list_cards_requires_auth():
    # No Authorization header -> 401 before any DB access.
    resp = client.get("/cards")
    assert resp.status_code == 401


def test_list_cards_rejects_garbage_token():
    resp = client.get("/cards", headers={"Authorization": "Bearer not-a-real-jwt"})
    assert resp.status_code == 401


# --- POST /cards login check (the body is valid so only login is tested) ---
_VALID_BODY = {"player": "X", "year": "2024", "set_name": "Topps", "category": "Basketball"}


def test_create_card_requires_auth():
    resp = client.post("/cards", json=_VALID_BODY)
    assert resp.status_code == 401


def test_create_card_rejects_garbage_token():
    resp = client.post(
        "/cards", json=_VALID_BODY, headers={"Authorization": "Bearer nope"}
    )
    assert resp.status_code == 401
