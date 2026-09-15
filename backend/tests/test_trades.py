"""Endpoint tests for POST /trades.

The existing `fake_db` fixture models ONE table. A trade touches four
(cards read, trades insert, cards insert, trade_items insert, cards update),
so this module ships its own multi-table fake.

What these tests are really guarding: a trade must move basis and realize
nothing. If any of them start reporting revenue on a trade, the bug that
corrupted the old spreadsheet is back.
"""

from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


# --- multi-table in-memory fake -------------------------------------------
class _Resp:
    def __init__(self, data):
        self.data = data


class _Q:
    def __init__(self, db, table):
        self.db, self.table_name = db, table
        self._filters = {}
        self._mode = "select"
        self._payload = None

    def insert(self, row):
        self._mode, self._payload = "insert", row
        return self

    def update(self, row):
        self._mode, self._payload = "update", row
        return self

    def select(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def in_(self, col, vals):
        self._filters[col] = vals
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def execute(self):
        if self.db.fail_on == self.table_name:
            raise RuntimeError("boom")
        if self._mode == "insert":
            self.db.inserts.setdefault(self.table_name, []).append(self._payload)
            n = len(self.db.inserts[self.table_name])
            return _Resp([{**self._payload, "id": f"{self.table_name}-{n}"}])
        if self._mode == "update":
            self.db.updates.append((self.table_name, self._filters, self._payload))
            return _Resp([{**self._filters, **self._payload}])
        rows = self.db.tables.get(self.table_name, [])
        for col, val in self._filters.items():
            want = val if isinstance(val, (list, tuple)) else [val]
            rows = [r for r in rows if r.get(col) in want]
        return _Resp(rows)


class _Client:
    def __init__(self, db):
        self.db = db

    def table(self, name):
        return _Q(self.db, name)


class MultiDB:
    def __init__(self):
        self.tables = {"cards": [], "grading_submissions": []}
        self.inserts = {}
        self.updates = []
        self.fail_on = None


@pytest.fixture
def db():
    d = MultiDB()
    factory = lambda token: _Client(d)  # noqa: E731
    with patch("app.routers.trades.user_client", factory), \
         patch("app.routers.cards.user_client", factory):
        yield d


def card(cid, price="100.00", **kw):
    base = {
        "id": cid, "player": "P", "year": "2024", "set_name": "S",
        "category": "basketball", "purchase_price": price, "status": "in_hand",
        "purchase_date": "2026-01-01",
    }
    base.update(kw)
    return base


def incoming(**kw):
    base = {"player": "New", "year": "2025", "set_name": "Prizm", "category": "basketball"}
    base.update(kw)
    return base


def post(payload):
    return client.post("/trades", json=payload)


# --- the core guarantee ----------------------------------------------------
def test_basis_carries_and_nothing_is_realized(auth, db):
    db.tables["cards"] = [card("A", "100.00")]
    r = post({
        "trade_date": "2026-09-14",
        "given_card_ids": ["A"],
        "received": [incoming(est_value="500")],
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert Decimal(body["total_basis"]) == Decimal("100.00")
    assert Decimal(body["realized_gain"]) == Decimal("0.00")
    # The received card's purchase_price IS the carried basis.
    assert db.inserts["cards"][0]["purchase_price"] == "100.00"
    assert db.inserts["cards"][0]["acquisition_source"] == "trade"


def test_all_in_cost_not_just_purchase_price_carries(auth, db):
    """Shipping and tax paid on the ORIGINAL card must carry too."""
    db.tables["cards"] = [
        card("A", "100.00", shipping_in="8.00", purchase_tax="7.00", other_costs="5.00")
    ]
    r = post({
        "trade_date": "2026-09-14",
        "given_card_ids": ["A"],
        "received": [incoming(est_value="1")],
    })
    assert Decimal(r.json()["total_basis"]) == Decimal("120.00")


def test_cash_paid_increases_carried_basis(auth, db):
    db.tables["cards"] = [card("A", "100.00")]
    r = post({
        "trade_date": "2026-09-14", "given_card_ids": ["A"],
        "received": [incoming(est_value="1")], "cash_boot": "50.00",
    })
    assert Decimal(r.json()["total_basis"]) == Decimal("150.00")


def test_cash_received_beyond_basis_reports_realized_gain(auth, db):
    db.tables["cards"] = [card("A", "30.00")]
    r = post({
        "trade_date": "2026-09-14", "given_card_ids": ["A"],
        "received": [incoming(est_value="1")], "cash_boot": "-100.00",
    })
    body = r.json()
    assert Decimal(body["total_basis"]) == Decimal("0.00")
    assert Decimal(body["realized_gain"]) == Decimal("70.00")
    assert any("realized gain" in w for w in body["warnings"])


def test_multi_card_allocation_is_pro_rata(auth, db):
    db.tables["cards"] = [card("A", "400.00")]
    r = post({
        "trade_date": "2026-09-14", "given_card_ids": ["A"],
        "received": [incoming(est_value="300"), incoming(est_value="100")],
    })
    prices = [c["purchase_price"] for c in db.inserts["cards"]]
    assert prices == ["300.00", "100.00"]


def test_missing_est_values_warn_about_even_split(auth, db):
    db.tables["cards"] = [card("A", "100.00")]
    r = post({
        "trade_date": "2026-09-14", "given_card_ids": ["A"],
        "received": [incoming(), incoming()],
    })
    body = r.json()
    assert body["even_split_fallback"] is True
    assert any("evenly" in w for w in body["warnings"])


# --- lineage + state -------------------------------------------------------
def test_given_card_is_marked_traded_away_and_lineage_written(auth, db):
    db.tables["cards"] = [card("A")]
    post({
        "trade_date": "2026-09-14", "given_card_ids": ["A"],
        "received": [incoming(est_value="1")],
    })
    assert ("cards", {"id": "A"}, {"status": "traded_away"}) in db.updates
    dirs = [i["direction"] for i in db.inserts["trade_items"]]
    assert dirs == ["given", "received"]
    assert db.inserts["trade_items"][1]["allocated_basis"] == "100.00"


def test_trade_row_records_signed_boot(auth, db):
    db.tables["cards"] = [card("A")]
    post({
        "trade_date": "2026-09-14", "given_card_ids": ["A"],
        "received": [incoming(est_value="1")], "cash_boot": "-25.00",
    })
    assert db.inserts["trades"][0]["cash_boot"] == "-25.00"


# --- refusals --------------------------------------------------------------
def test_unknown_card_is_404(auth, db):
    r = post({
        "trade_date": "2026-09-14", "given_card_ids": ["nope"],
        "received": [incoming(est_value="1")],
    })
    assert r.status_code == 404


def test_already_traded_card_is_409(auth, db):
    """Trading the same card twice would create basis out of nothing."""
    db.tables["cards"] = [card("A", status="traded_away")]
    r = post({
        "trade_date": "2026-09-14", "given_card_ids": ["A"],
        "received": [incoming(est_value="1")],
    })
    assert r.status_code == 409


def test_no_received_cards_is_rejected(auth, db):
    r = post({
        "trade_date": "2026-09-14", "given_card_ids": ["A"], "received": [],
    })
    assert r.status_code == 422


def test_no_given_cards_is_rejected(auth, db):
    r = post({
        "trade_date": "2026-09-14", "given_card_ids": [],
        "received": [incoming(est_value="1")],
    })
    assert r.status_code == 422


def test_position_type_defaults_to_flip_on_traded_in_card(auth, db):
    """A traded-in card is a normal card — the hold rule still applies."""
    db.tables["cards"] = [card("A")]
    post({
        "trade_date": "2026-09-14", "given_card_ids": ["A"],
        "received": [incoming(est_value="1")],
    })
    assert db.inserts["cards"][0]["position_type"] == "flip"


def test_trade_insert_failure_does_not_move_any_card(auth, db):
    """The destructive step runs LAST, so an early failure moves nothing."""
    db.tables["cards"] = [card("A")]
    db.fail_on = "trades"
    r = post({
        "trade_date": "2026-09-14", "given_card_ids": ["A"],
        "received": [incoming(est_value="1")],
    })
    assert r.status_code == 502
    assert db.updates == []
    assert "cards" not in db.inserts
