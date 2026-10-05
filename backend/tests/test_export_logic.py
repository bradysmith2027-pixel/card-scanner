"""
test_export_logic.py

Tests for GET /export/csv with fake cards. They check the CSV columns and the
money math that replaced my old Excel formula.

The export doesn't calculate profit itself anymore, it uses app.profit. Two of
these tests used to check the old way:

  - the 11 column header (now 28 columns, with the cost breakdown next to the
    profit so you can see where the number came from)
  - profit = sale_price - purchase_price

The old profit test would actually still pass, because with no fees in the
test data, the right answer and the wrong answer are the same. That's why the
bug lasted so long. None of the tests had fees in them. The fee test below is
the one that was missing.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app

pytestmark = pytest.mark.unit

client = TestClient(app)

_HEADER = [
    # serial came with migration 010. It's next to card_number because they're
    # easy to mix up, and side by side it's obvious if one is in the wrong column.
    "player", "year", "set_name", "card_number", "serial", "card_type", "category",
    "position_type", "lane",
    "purchase_price", "shipping_in", "purchase_tax", "other_costs",
    "grading_cost", "all_in_cost",
    "status", "purchase_date", "sale_date", "hold_days",
    "sale_channel", "sale_price", "shipping_collected",
    "platform_fees", "shipping_out", "net_proceeds",
    "net_profit", "roi_pct",
    "est_market_value", "created_at",
]
_NET_PROFIT_IDX = _HEADER.index("net_profit")
_ALL_IN_IDX = _HEADER.index("all_in_cost")
_ROI_IDX = _HEADER.index("roi_pct")


def test_header_uses_renamed_columns(auth, fake_db):
    fake_db.select_rows = []
    resp = client.get("/export/csv")
    assert resp.status_code == 200
    header = resp.text.splitlines()[0].split(",")
    assert header == _HEADER
    # The old column names from before migration 003 shouldn't come back.
    assert "sport" not in header and "variation" not in header
    # And the old profit column without fees is gone.
    assert "profit" not in header


def test_net_profit_when_sold_with_no_fees_equals_gross(auth, fake_db):
    """With no fees the old and new math give the same answer. This is what hid the bug."""
    fake_db.select_rows = [{
        "id": "c1",
        "player": "Luka Doncic", "year": "2018", "set_name": "Prizm",
        "category": "basketball", "purchase_price": 100, "sale_price": 250,
        "status": "sold", "created_at": "2026-01-01",
    }]
    resp = client.get("/export/csv")
    row = resp.text.splitlines()[1].split(",")
    assert row[_NET_PROFIT_IDX] == "150.00"
    assert row[_ALL_IN_IDX] == "100.00"


def test_fees_and_shipping_reduce_net_profit(auth, fake_db):
    """The main test for this file.

    The export used to just do sale_price - purchase_price and ignore every
    other cost. This fails if that ever comes back.
    """
    fake_db.select_rows = [{
        "id": "c1",
        "player": "Ohtani", "year": "2024", "set_name": "Topps",
        "category": "baseball", "status": "sold",
        "purchase_price": 100, "shipping_in": 5, "purchase_tax": 7,
        "sale_price": 250, "platform_fees": 33.13, "shipping_out": 5,
        "created_at": "2026-01-01",
    }]
    resp = client.get("/export/csv")
    row = resp.text.splitlines()[1].split(",")

    assert row[_ALL_IN_IDX] == "112.00"       # 100 + 5 + 7
    assert row[_NET_PROFIT_IDX] == "99.87"    # (250 - 33.13 - 5) - 112
    assert row[_NET_PROFIT_IDX] != "150.00"   # what the old code would say


def test_shipping_collected_counts_as_revenue(auth, fake_db):
    fake_db.select_rows = [{
        "id": "c1", "player": "x", "category": "baseball", "status": "sold",
        "purchase_price": 10, "sale_price": 20,
        "shipping_collected": 6, "shipping_out": 5,
    }]
    resp = client.get("/export/csv")
    row = resp.text.splitlines()[1].split(",")
    assert row[_NET_PROFIT_IDX] == "11.00"  # (20 + 6 - 5) - 10


def test_profit_blank_when_unsold(auth, fake_db):
    """Blank, not 0. A spreadsheet averages in a 0 but skips blanks."""
    fake_db.select_rows = [{
        "id": "c1",
        "player": "Anthony Edwards", "purchase_price": 40, "sale_price": None,
        "category": "basketball", "status": "in_hand",
    }]
    resp = client.get("/export/csv")
    row = resp.text.splitlines()[1].split(",")
    assert row[_NET_PROFIT_IDX] == ""
    assert row[_ROI_IDX] == ""


def test_unsold_card_with_a_sale_price_still_has_no_profit(auth, fake_db):
    """The other old bug: it showed a profit for any card with both prices,
    even one I still had."""
    fake_db.select_rows = [{
        "id": "c1", "player": "x", "category": "baseball",
        "status": "in_hand", "purchase_price": 40, "sale_price": 90,
    }]
    resp = client.get("/export/csv")
    row = resp.text.splitlines()[1].split(",")
    assert row[_NET_PROFIT_IDX] == ""


def test_roi_is_rendered_as_percent(auth, fake_db):
    fake_db.select_rows = [{
        "id": "c1", "player": "x", "category": "baseball", "status": "sold",
        "purchase_price": 100, "sale_price": 150,
    }]
    resp = client.get("/export/csv")
    row = resp.text.splitlines()[1].split(",")
    assert row[_ROI_IDX] == "50.00"


def test_empty_inventory_header_only(auth, fake_db):
    fake_db.select_rows = []
    resp = client.get("/export/csv")
    assert resp.status_code == 200
    assert len(resp.text.strip().splitlines()) == 1  # header, no data rows
