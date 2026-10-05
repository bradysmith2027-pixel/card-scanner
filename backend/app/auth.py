"""
auth.py

Checks the Supabase login token on every request and figures out who's calling.

Every protected endpoint uses current_user, which:
  1. Grabs the Bearer token from the Authorization header.
  2. Checks the signature and expiration against Supabase's public key.
  3. Returns the user id and token.

The token also gets passed to supabase_client.user_client() so the database
runs queries as that user. So there's a check here and RLS in the database too.

My Supabase project signs tokens with ES256 (not the old shared secret). The
public keys are at:
    {SUPABASE_URL}/auth/v1/.well-known/jwks.json
PyJWKClient downloads and caches them and picks the right one for each token.
"""

import logging
from dataclasses import dataclass
from functools import lru_cache

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

from app.config import get_settings

logger = logging.getLogger(__name__)

# auto_error=False so I can send back my own 401 instead of FastAPI's default.
_bearer = HTTPBearer(auto_error=False)

# Supabase tokens use "authenticated" as the audience.
_AUDIENCE = "authenticated"
_ALGORITHMS = ["ES256"]


@dataclass
class AuthedUser:
    id: str          # Supabase user id (the "sub" in the token)
    token: str       # the token itself, sent to Supabase so RLS works
    readonly: bool = False   # True for view-only accounts, blocks any changes


@lru_cache
def _jwks_client() -> PyJWKClient:
    """
    Client for Supabase's public keys. Only gets made once, and it caches the
    keys so it doesn't download them on every request.
    """
    settings = get_settings()
    settings.require("supabase_url")
    jwks_url = f"{settings.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"
    return PyJWKClient(jwks_url)


def current_user(
    request: Request,
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
        # Anything else that goes wrong (can't get the keys, SUPABASE_URL not
        # set, a broken token, etc). The user just gets a plain 401, but I log
        # the real error so I can actually tell what broke. When SUPABASE_URL
        # was missing on Railway, all I got was "Could not verify token."
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

    # Email allowlist (ALLOWED_EMAILS).
    #
    # Having a valid token isn't enough. Anyone with a Google account can sign
    # up through Supabase, so the allowlist is what actually keeps people out.
    #
    #   ALLOWED_EMAILS set               -> only those emails get in
    #   not set + ALLOW_OPEN_ACCESS=true -> anyone can get in (on purpose)
    #   not set at all                   -> nobody gets in
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

    # Read-only accounts.
    #
    # I check this here based on the request method instead of on each route.
    # That way if I add a new endpoint later I can't forget to block viewers
    # on it.
    #
    # RLS (migration 011) is what protects my data. This just stops a viewer
    # from adding cards of their own, which RLS would allow.
    email = (payload.get("email") or "").lower()
    readonly = bool(email) and email in settings.readonly_emails
    if readonly and request.method not in ("GET", "HEAD", "OPTIONS"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account has read-only access.",
        )

    return AuthedUser(id=user_id, token=token, readonly=readonly)
