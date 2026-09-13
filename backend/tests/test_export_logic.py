"""
test_export_logic.py — mocked unit tests for GET /export/csv.

No DB: the cards list is faked. These pin down the CSV contract and the money
logic that replaces the old Excel formula.

UPDATED 2026-09-13. The export no longer computes profit itself — it consumes
`app.profit`, the single definition. Two of these tests previously asserted the
OLD gross behaviour:

  - the 11-column header (now 28 columns, with the cost breakdown beside the
    result so a number is never unexplained)
  - `profit = sale_price - purchase_price`

Note the old profit test would still have passed on its arithmetic, because
with no fee data net and gross are identical. That is precisely why the bug
survived so long: every test fixture omitted fees, so the gross formula looked
correct. The fee test below is the guard that was missing.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app

pytestmark = pytest.mark.unit

client = TestClient(app)

_HEADER = [
    "player", "year", "set_name", "card_number", "card_type", "category",
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
    # The 003 renames must never come back.
    assert "sport" not in header and "variation" not in header
    # And the bare gross column is gone for good.
    assert "profit" not in header


def test_net_profit_when_sold_with_no_fees_equals_gross(auth, fake_db):
    """With zero fees, net == gross. This is the benign case that hid the bug."""
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
    """🔴 THE REGRESSION GUARD.

    The export used to report `sale_price - purchase_price` and ignore every
    cost. This is the test that fails if that ever comes back.
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
    assert row[_NET_PROFIT_IDX] != "150.00"   # the old gross answer


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
    """Blank, not 0 — a spreadsheet averages 0 but skips blanks."""
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
    """The second bug in the old line: it emitted a profit for ANY card with
    both prices, including one still sitting in hand."""
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
