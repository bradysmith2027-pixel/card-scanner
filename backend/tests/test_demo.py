"""
test_demo.py

Tests for GET /demo/cards, the one endpoint with no login.

Most of these are negative tests. The risk with a public endpoint isn't that
it fails to work, it's that it quietly works too well and hands out something
it shouldn't. So these check what the response does NOT contain, and that the
endpoint cannot be pointed at the real cards table.
"""

from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import demo
from app.routers.demo import PUBLIC_FIELDS

client = TestClient(app)


# A row shaped like demo_cards: sold, with fees and shipping, so the profit
# math has something real to do.
SOLD_ROW = {
    "id": "demo-1",
    "player": "Victor Wembanyama",
    "year": "2023",
    "set_name": "Prizm",
    "card_number": "136",
    "serial": None,
    "category": "basketball",
    "card_type": "Silver",
    "status": "sold",
    "purchase_price": 100,
    "purchase_date": "2026-01-10",
    "sale_price": 200,
    "sale_date": "2026-02-10",
    "shipping_in": 5,
    "purchase_tax": 0,
    "other_costs": 0,
    "shipping_out": 5,
    "platform_fees": 26,
    "shipping_collected": 6,
    "sale_channel": "ebay",
    "position_type": "flip",
    "lane": None,
    "est_market_value": None,
    "grading_cost": 8,
    "image_url": None,
}

UNSOLD_ROW = {**SOLD_ROW, "id": "demo-2", "status": "in_hand",
              "sale_price": None, "sale_date": None, "platform_fees": None,
              "shipping_out": None, "shipping_collected": None}


class _FakeTable:
    def __init__(self, rows, recorder):
        self._rows = rows
        self._rec = recorder

    def select(self, cols):
        self._rec["selected"] = cols
        return self

    def order(self, *a, **k):
        return self

    def limit(self, n):
        self._rec["limit"] = n
        return self

    def execute(self):
        class R:
            data = self._rows
        return R()


class _FakeClient:
    def __init__(self, rows, recorder):
        self._rows = rows
        self._rec = recorder

    def table(self, name):
        self._rec["table"] = name
        return _FakeTable(self._rows, self._rec)


@pytest.fixture
def fake_demo_db():
    """Swap the anon client for a fake and record what it was asked for."""
    rec = {}

    def _factory(rows):
        patcher = patch.object(demo, "_anon_client", lambda: _FakeClient(rows, rec))
        patcher.start()
        return patcher, rec

    return _factory


def test_demo_cards_needs_no_login(fake_demo_db):
    """The whole point: a stranger with the link gets data, not a 401."""
    patcher, _ = fake_demo_db([SOLD_ROW])
    try:
        r = client.get("/demo/cards")  # no Authorization header at all
        assert r.status_code == 200
        assert r.json()["count"] == 1
    finally:
        patcher.stop()


def test_demo_reads_demo_cards_never_cards(fake_demo_db):
    """The table name is the first line of defence. Pin it.

    If someone ever 'simplifies' this endpoint by pointing it at cards, this
    fails before it reaches production.
    """
    patcher, rec = fake_demo_db([SOLD_ROW])
    try:
        client.get("/demo/cards")
        assert rec["table"] == "demo_cards"
        assert rec["table"] != "cards"
    finally:
        patcher.stop()


def test_demo_never_returns_private_fields(fake_demo_db):
    """Even if the row somehow carries private columns, they must not ship.

    This feeds the endpoint a row polluted with every field the demo is
    supposed to drop, which is the situation that would actually hurt.
    """
    polluted = {
        **SOLD_ROW,
        "user_id": "real-user-uuid",
        "buyer_name": "Some Buyer",
        "notes": "paid under comp, seller was motivated",
        "hold_thesis": "waiting on a rookie bump",
        "ebay_comp_url": "https://ebay.com/itm/123",
    }
    patcher, _ = fake_demo_db([polluted])
    try:
        body = client.get("/demo/cards").text
        for leaked in (
            "real-user-uuid",
            "Some Buyer",
            "motivated",
            "rookie bump",
            "ebay.com/itm",
        ):
            assert leaked not in body, f"{leaked!r} leaked into the demo response"
    finally:
        patcher.stop()


def test_select_is_an_allow_list(fake_demo_db):
    """Columns are requested by name, so a new column isn't exposed by default."""
    patcher, rec = fake_demo_db([SOLD_ROW])
    try:
        client.get("/demo/cards")
        assert rec["selected"] != "*", "demo must not select *"
        for field in ("user_id", "buyer_name", "notes", "hold_thesis"):
            assert field not in rec["selected"]
    finally:
        patcher.stop()


def test_demo_profit_matches_the_real_math(fake_demo_db):
    """The demo must run the same profit code, fees and all.

    all_in = 100 + 5 + 8 grading = 113
    proceeds = 200 + 6 - 26 - 5 = 175
    net = 62
    This is the exact case from the real fix. Gross would say +100.
    """
    patcher, _ = fake_demo_db([SOLD_ROW])
    try:
        card = client.get("/demo/cards").json()["cards"][0]
        assert Decimal(str(card["all_in_cost"])) == Decimal("113.00")
        assert Decimal(str(card["net_proceeds"])) == Decimal("175.00")
        assert Decimal(str(card["net_profit"])) == Decimal("62.00")
        assert Decimal(str(card["net_profit"])) != Decimal("100.00")
    finally:
        patcher.stop()


def test_unsold_card_has_no_profit(fake_demo_db):
    """None, not 0 - same rule as the real inventory."""
    patcher, _ = fake_demo_db([UNSOLD_ROW])
    try:
        card = client.get("/demo/cards").json()["cards"][0]
        assert card["net_profit"] is None
        assert card["roi"] is None
    finally:
        patcher.stop()


def test_response_says_the_numbers_are_scaled(fake_demo_db):
    """Anyone reading the API directly should be told, not just the UI."""
    patcher, _ = fake_demo_db([SOLD_ROW])
    try:
        notice = client.get("/demo/cards").json()["notice"].lower()
        assert "demo" in notice
        assert "scaled" in notice
    finally:
        patcher.stop()


def test_public_fields_cannot_include_private_columns():
    """Guard the allow-list itself, not just one response."""
    banned = {"user_id", "buyer_name", "notes", "hold_thesis", "ebay_comp_url"}
    assert banned.isdisjoint(set(PUBLIC_FIELDS))


def test_demo_does_not_use_the_service_role_key():
    """The service role skips RLS. It must never back a public endpoint.

    Checks the parsed syntax tree, not the raw text. A plain substring search
    trips over the comment in demo.py that explains why service_client is the
    wrong choice here, so it would fail on code that is actually correct.
    What matters is whether it's imported or called.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(demo))

    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "service_client" not in imported, (
        "demo.py must not import service_client - it bypasses RLS"
    )

    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "service_client" not in called, (
        "demo.py must not call service_client - it bypasses RLS"
    )
