"""
auth.py — FastAPI dependency that verifies a Supabase JWT and identifies the
caller.

Every protected endpoint depends on `current_user`, which:
  1. Pulls the Bearer token from the Authorization header.
  2. Verifies its signature + expiry against Supabase's ES256 signing key.
  3. Returns an AuthedUser (id + raw token) for downstream use.

The raw token is passed on to supabase_client.user_client() so RLS runs as
this user — auth here and RLS in the database are belt-and-suspenders.

SIGNING KEYS (2026-07-21):
  This project uses asymmetric JWT signing keys — the CURRENT key is ECC
  (P-256), so access tokens are signed with **ES256**. Supabase publishes the
  matching public key(s) at:
      {SUPABASE_URL}/auth/v1/.well-known/jwks.json
  We verify against that JWKS (no shared secret). PyJWKClient fetches + caches
  the keys and selects the right one via the token's `kid` header. The old
  legacy HS256 shared secret is retired; since there are no pre-rotation users,
  we don't need an HS256 fallback path.
"""

import logging
from dataclasses import dataclass
from functools import lru_cache

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

from app.config import get_settings

logger = logging.getLogger(__name__)

# auto_error=False so we can return a clean 401 instead of FastAPI's default.
_bearer = HTTPBearer(auto_error=False)

# Supabase access tokens carry the "authenticated" audience.
_AUDIENCE = "authenticated"
_ALGORITHMS = ["ES256"]


@dataclass
class AuthedUser:
    id: str          # Supabase auth user id (the JWT "sub" claim)
    token: str       # raw access token, forwarded to Supabase for RLS


@lru_cache
def _jwks_client() -> PyJWKClient:
    """
    Cached client for Supabase's JWKS endpoint. Built once; PyJWKClient caches
    fetched signing keys internally so we're not hitting the network per request.
    """
    settings = get_settings()
    settings.require("supabase_url")
    jwks_url = f"{settings.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"
    return PyJWKClient(jwks_url)


def current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> AuthedUser:
    if creds is None or not creds.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token.",
        )

    token = creds.credentials

    try:
        signing_key = _jwks_client().get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=_ALGORITHMS,
            audience=_AUDIENCE,
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired."
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token."
        )
    except Exception:
        # JWKS fetch/parse failures, missing SUPABASE_URL, malformed token
        # headers, etc. The client gets a generic 401 (no internal detail
        # leaked), but the real cause is logged server-side — otherwise this
        # branch is undebuggable in production, which is exactly what happened
        # on 2026-08-24 when an unset SUPABASE_URL surfaced only as
        # "Could not verify token." Same lesson as the create_card fix (8/18).
        logger.exception("Token verification failed")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not verify token.",
        )

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing subject.",
        )

    # Access allowlist (env ALLOWED_EMAILS) — FAIL-CLOSED as of 2026-08-24.
    #
    # A valid token is NOT sufficient. Supabase signup is public and Google SSO
    # issues usable tokens with no email confirmation, so "authenticated" means
    # "any stranger with a Google account." The allowlist is the real gate.
    #
    #   ALLOWED_EMAILS set      -> only those emails pass
    #   unset + ALLOW_OPEN_ACCESS=true -> open, deliberately
    #   unset + no opt-in       -> DENY EVERYONE (a forgotten env var must never
    #                              silently open the API)
    settings = get_settings()
    if settings.allowed_emails:
        email = payload.get("email")
        if not email or email.lower() not in settings.allowed_emails:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access restricted to the owner.",
            )
    elif not settings.allow_open_access:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Access control is not configured. Set ALLOWED_EMAILS, or set "
                "ALLOW_OPEN_ACCESS=true to intentionally allow any signed-in user."
            ),
        )

    return AuthedUser(id=user_id, token=token)
