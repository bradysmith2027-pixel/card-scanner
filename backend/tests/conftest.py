"""
conftest.py — shared fixtures for the fast unit suite (Q1).

Goal: exercise real endpoint/business logic with every external service mocked,
so these tests run with NO network, NO database, and NO OpenAI spend.

Provides:
  - `auth`     : overrides the current_user dependency so requests are treated
                 as an authenticated test user (skips real JWT/JWKS verification).
  - `fake_db`  : patches supabase user_client() in the cards + export routers
                 with an in-memory fake, and hands the test a knob to set the
                 rows a query returns and inspect what got inserted.

The slowapi rate limiter is disabled here so /scan tests don't 429.
"""

import pytest
from unittest.mock import patch

from app.main import app
from app.auth import AuthedUser, current_user
from app.rate_limit import limiter

# No rate limiting during unit tests (otherwise repeated /scan calls could 429).
limiter.enabled = False

TEST_USER_ID = "test-user-123"


@pytest.fixture
def auth():
    """Treat every request as this authenticated user (bypasses JWT verify)."""
    app.dependency_overrides[current_user] = lambda: AuthedUser(
        id=TEST_USER_ID, token="fake-token"
    )
    yield TEST_USER_ID
    app.dependency_overrides.pop(current_user, None)


# --- In-memory fake of the Supabase query builder --------------------------
class _Resp:
    def __init__(self, data):
        self.data = data


class _InsertQuery:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return _Resp(self._result)


class _Query:
    def __init__(self, db):
        self._db = db

    def insert(self, row):
        self._db.inserted = row
        result = self._db.insert_result
        if result == "echo":  # default: DB echoes the row back with an id
            result = [{**row, "id": "generated-id"}]
        return _InsertQuery(result)

    def select(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def eq(self, col, val):
        self._db.eq_filters.append((col, val))
        return self

    def execute(self):
        return _Resp(self._db.select_rows)


class _Client:
    def __init__(self, db):
        self._db = db

    def table(self, name):
        self._db.table_name = name
        return _Query(self._db)


class FakeDB:
    """Test knobs: set `select_rows` / `insert_result`; read `inserted`, etc."""

    def __init__(self):
        self.select_rows = []       # what a SELECT ... execute() returns
        self.insert_result = "echo"  # "echo" = return inserted row + id; or set a list
        self.inserted = None         # the row passed to .insert()
        self.eq_filters = []         # (column, value) filters applied
        self.table_name = None       # last table() name


@pytest.fixture
def fake_db():
    db = FakeDB()

    def _factory(token):  # matches user_client(token)
        return _Client(db)

    with patch("app.routers.cards.user_client", _factory), \
         patch("app.routers.export.user_client", _factory):
        yield db
