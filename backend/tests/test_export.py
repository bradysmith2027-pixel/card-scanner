"""test_export.py — offline auth-gate tests for GET /export/csv."""

import pytest
from fastapi.testclient import TestClient

from app.main import app

pytestmark = pytest.mark.unit

client = TestClient(app)


def test_export_requires_auth():
    assert client.get("/export/csv").status_code == 401


def test_export_rejects_garbage_token():
    r = client.get("/export/csv", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401
