"""
test_scan.py — offline tests for POST /scan (no model/GPT-4o calls).

The auth gate and validation short-circuit before any Roboflow/OpenAI work, so
these run without network or spend. Full pipeline is covered by the live check.
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

_TINY = ("f.jpg", b"not-a-real-image", "image/jpeg")


def test_scan_requires_auth():
    # Valid-looking multipart, no token -> 401 before any scan work.
    resp = client.post(
        "/scan",
        files={"front": _TINY},
        data={"capture_mode": "sports"},
    )
    assert resp.status_code == 401


def test_scan_rejects_garbage_token():
    resp = client.post(
        "/scan",
        files={"front": _TINY},
        data={"capture_mode": "sports"},
        headers={"Authorization": "Bearer nope"},
    )
    assert resp.status_code == 401
