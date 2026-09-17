"""
test_readonly_viewer.py — the READONLY_EMAILS guard (2026-09-17).

WHY THESE TESTS EXIST
    A permission check with no test is not a permission check. The specific
    failure this guards against is the one CLAUDE.md already records from
    2026-09-13: a test that still passes under broken code. So the important
    assertions here are the NEGATIVE ones — that a write is actually refused —
    and `test_guard_is_not_vacuous`, which fails if READONLY_EMAILS stops being
    read at all.

    These call `current_user` directly rather than going through the API,
    because conftest.py overrides `current_user` for every route test. A test
    driving the endpoints would exercise the override, not the real dependency,
    and would pass no matter what this code did.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import auth

pytestmark = pytest.mark.unit

OWNER = "owner@example.com"
VIEWER = "viewer@example.com"


class _Settings(SimpleNamespace):
    """Minimal stand-in for the real Settings object."""


def _settings(readonly=(), allowed=(OWNER, VIEWER)):
    return _Settings(
        allowed_emails=[e.lower() for e in allowed],
        allow_open_access=False,
        readonly_emails=[e.lower() for e in readonly],
    )


def _call(monkeypatch, *, email, method, readonly_emails=(VIEWER,)):
    """Invoke current_user with a forged-but-already-verified payload."""
    monkeypatch.setattr(auth, "get_settings", lambda: _settings(readonly_emails))
    monkeypatch.setattr(
        auth, "_decode", lambda token: {"sub": "uid-123", "email": email}, raising=False
    )
    # Bypass signature verification — that path has its own coverage; here we
    # care only about what happens AFTER a token is known to be valid.
    monkeypatch.setattr(
        auth.jwt, "decode", lambda *a, **k: {"sub": "uid-123", "email": email}
    )
    monkeypatch.setattr(
        auth, "_jwks_client", lambda: SimpleNamespace(
            get_signing_key_from_jwt=lambda t: SimpleNamespace(key="k")
        )
    )
    request = SimpleNamespace(method=method)
    creds = SimpleNamespace(credentials="tok")
    return auth.current_user(request, creds)


# --------------------------------------------------------------------------
# The viewer can read.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_viewer_may_read(monkeypatch, method):
    user = _call(monkeypatch, email=VIEWER, method=method)
    assert user.readonly is True


# --------------------------------------------------------------------------
# The viewer cannot write. These are the assertions that matter.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("method", ["POST", "PATCH", "PUT", "DELETE"])
def test_viewer_write_is_refused(monkeypatch, method):
    with pytest.raises(HTTPException) as exc:
        _call(monkeypatch, email=VIEWER, method=method)
    assert exc.value.status_code == 403
    assert "read-only" in exc.value.detail.lower()


def test_viewer_cannot_write_even_to_their_own_rows(monkeypatch):
    """RLS permits a viewer to insert rows under their OWN user_id.

    That is the gap this guard closes: without it, an account described to the
    user as 'read-only' can still create data. The 403 must come from the API,
    because the database will not object.
    """
    with pytest.raises(HTTPException) as exc:
        _call(monkeypatch, email=VIEWER, method="POST")
    assert exc.value.status_code == 403


# --------------------------------------------------------------------------
# The owner is unaffected.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("method", ["GET", "POST", "PATCH", "DELETE"])
def test_owner_is_never_restricted(monkeypatch, method):
    user = _call(monkeypatch, email=OWNER, method=method)
    assert user.readonly is False


def test_email_match_is_case_insensitive(monkeypatch):
    with pytest.raises(HTTPException):
        _call(monkeypatch, email=VIEWER.upper(), method="POST")


# --------------------------------------------------------------------------
# The guard must not be vacuous.
# --------------------------------------------------------------------------

def test_guard_is_not_vacuous(monkeypatch):
    """With READONLY_EMAILS empty, the same POST must SUCCEED.

    Without this, every assertion above would still pass if the guard rejected
    everything, or if readonly_emails were never consulted. This is the control
    that makes the rest of the file meaningful.
    """
    user = _call(monkeypatch, email=VIEWER, method="POST", readonly_emails=())
    assert user.readonly is False
