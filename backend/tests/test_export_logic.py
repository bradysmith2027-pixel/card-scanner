"""
test_export_logic.py — mocked unit tests for GET /export/csv.

No DB: the cards list is faked. These pin down the CSV contract and the profit
math (the money logic that replaces the old Excel formula):
  - profit = sale_price - purchase_price only when BOTH are present, else blank
  - header uses the renamed columns (card_type, category), not sport/variation
  - empty inventory returns just the header row
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app

pytestmark = pytest.mark.unit

client = TestClient(app)

_HEADER = [
    "player", "year", "set_name", "card_number", "card_type", "category",
    "purchase_price", "sale_price", "status", "profit", "created_at",
]
_PROFIT_IDX = _HEADER.index("profit")


def test_header_uses_renamed_columns(auth, fake_db):
    fake_db.select_rows = []
    resp = client.get("/export/csv")
    assert resp.status_code == 200
    header = resp.text.splitlines()[0].split(",")
    assert header == _HEADER
    assert "sport" not in header and "variation" not in header


def test_profit_computed_when_sold(auth, fake_db):
    fake_db.select_rows = [{
        "player": "Luka Doncic", "year": "2018", "set_name": "Prizm",
        "category": "basketball", "purchase_price": 100, "sale_price": 250,
        "status": "sold", "created_at": "2026-01-01",
    }]
    resp = client.get("/export/csv")
    row = resp.text.splitlines()[1].split(",")
    assert row[_PROFIT_IDX] == "150"  # 250 - 100


def test_profit_blank_when_unsold(auth, fake_db):
    fake_db.select_rows = [{
        "player": "Anthony Edwards", "purchase_price": 40, "sale_price": None,
        "category": "basketball", "status": "in_hand",
    }]
    resp = client.get("/export/csv")
    row = resp.text.splitlines()[1].split(",")
    assert row[_PROFIT_IDX] == ""  # only one price -> no profit


def test_empty_inventory_header_only(auth, fake_db):
    fake_db.select_rows = []
    resp = client.get("/export/csv")
    assert resp.status_code == 200
    assert len(resp.text.strip().splitlines()) == 1  # header, no data rows
