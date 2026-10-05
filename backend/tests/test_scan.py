"""
test_scan.py

Offline tests for POST /scan. No model or GPT-4o calls.

The login check and file checks happen before Roboflow or OpenAI, so these
don't need the internet or cost anything. The live check script tests the full
scan.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app

pytestmark = pytest.mark.unit

client = TestClient(app)

_TINY = ("f.jpg", b"not-a-real-image", "image/jpeg")


def test_scan_requires_auth():
    # Normal looking upload but no token -> 401 before anything runs.
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
