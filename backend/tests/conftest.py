"""
conftest.py

Shared fixtures for the unit tests.

Everything outside the app is faked, so the tests run without the internet,
the database, or spending anything on OpenAI.

  - auth:    replaces current_user so every request counts as a logged in test
             user (skips the real token check).
  - fake_db: swaps user_client() for a fake in-memory one, so a test can set
             what a query returns and check what got inserted.

The rate limiter is turned off so the /scan tests don't get a 429.
"""

import pytest
from unittest.mock import patch

from app.main import app
from app.auth import AuthedUser, current_user
from app.rate_limit import limiter

# No rate limiting in tests, otherwise a bunch of /scan calls could get a 429.
limiter.enabled = False

TEST_USER_ID = "test-user-123"


@pytest.fixture
def auth():
    """Treat every request as this logged in user (skips the token check)."""
    app.dependency_overrides[current_user] = lambda: AuthedUser(
        id=TEST_USER_ID, token="fake-token"
    )
    yield TEST_USER_ID
    app.dependency_overrides.pop(current_user, None)


# --- Fake version of the Supabase query builder ----------------------------
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
        if result == "echo":  # default: send the row back with an id like the DB would
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
    """Set select_rows / insert_result before a test, then check inserted, etc."""

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
