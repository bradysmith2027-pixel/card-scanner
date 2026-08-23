"""
test_cards.py — endpoint tests that don't require a live DB or real token.

Auth-gate and wiring checks run offline: the 401 path short-circuits in the
current_user dependency before any Supabase call. DB-backed behavior (RLS
isolation, real rows) needs an integration test with a real JWT — added later.
"""

from fastapi.testclient import TestClient

from app.main import app

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


# --- POST /cards auth gate (valid body so only auth is under test) ---------
_VALID_BODY = {"player": "X", "year": "2024", "set_name": "Topps", "category": "Basketball"}


def test_create_card_requires_auth():
    resp = client.post("/cards", json=_VALID_BODY)
    assert resp.status_code == 401


def test_create_card_rejects_garbage_token():
    resp = client.post(
        "/cards", json=_VALID_BODY, headers={"Authorization": "Bearer nope"}
    )
    assert resp.status_code == 401
