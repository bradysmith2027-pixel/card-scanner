"""
test_readonly_viewer.py

Tests for read-only accounts (READONLY_EMAILS).

The tests that matter most are the ones checking that changes actually get
blocked, plus test_guard_is_not_vacuous, which fails if READONLY_EMAILS stops
being checked at all. I've had a test pass on broken code before, so I want
to be sure these can actually fail.

These call current_user directly instead of going through the API, because
conftest.py replaces current_user for all the route tests. Going through the
API would just test the fake version and pass no matter what.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import auth

pytestmark = pytest.mark.unit

OWNER = "owner@example.com"
VIEWER = "viewer@example.com"


class _Settings(SimpleNamespace):
    """A simple fake of the real Settings object."""


def _settings(readonly=(), allowed=(OWNER, VIEWER)):
    return _Settings(
        allowed_emails=[e.lower() for e in allowed],
        allow_open_access=False,
        readonly_emails=[e.lower() for e in readonly],
    )


def _call(monkeypatch, *, email, method, readonly_emails=(VIEWER,)):
    """Call current_user with a made up token that counts as already checked."""
    monkeypatch.setattr(auth, "get_settings", lambda: _settings(readonly_emails))
    monkeypatch.setattr(
        auth, "_decode", lambda token: {"sub": "uid-123", "email": email}, raising=False
    )
    # Skip the signature check. That's tested somewhere else, this only cares
    # about what happens after the token is good.
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
# The viewer can't change anything. These are the important ones.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("method", ["POST", "PATCH", "PUT", "DELETE"])
def test_viewer_write_is_refused(monkeypatch, method):
    with pytest.raises(HTTPException) as exc:
        _call(monkeypatch, email=VIEWER, method=method)
    assert exc.value.status_code == 403
    assert "read-only" in exc.value.detail.lower()


def test_viewer_cannot_write_even_to_their_own_rows(monkeypatch):
    """RLS would let a viewer add cards under their own user_id.

    That's what this blocks. Without it a "read-only" account could still add
    stuff. The 403 has to come from the API since the database won't stop it.
    """
    with pytest.raises(HTTPException) as exc:
        _call(monkeypatch, email=VIEWER, method="POST")
    assert exc.value.status_code == 403


# --------------------------------------------------------------------------
# The owner (me) isn't affected.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("method", ["GET", "POST", "PATCH", "DELETE"])
def test_owner_is_never_restricted(monkeypatch, method):
    user = _call(monkeypatch, email=OWNER, method=method)
    assert user.readonly is False


def test_email_match_is_case_insensitive(monkeypatch):
    with pytest.raises(HTTPException):
        _call(monkeypatch, email=VIEWER.upper(), method="POST")


# --------------------------------------------------------------------------
# Make sure the check can actually fail.
# --------------------------------------------------------------------------

def test_guard_is_not_vacuous(monkeypatch):
    """With READONLY_EMAILS empty, the same POST should work.

    Without this, all the tests above would still pass if the check just blocked
    everyone, or if readonly_emails was never looked at.
    """
    user = _call(monkeypatch, email=VIEWER, method="POST", readonly_emails=())
    assert user.readonly is False
