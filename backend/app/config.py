"""
config.py — loads and validates environment configuration once at startup.

Reads from a local .env (if present) plus real environment variables. Fails
loudly at boot if a required secret is missing, rather than 500-ing later on
the first request that needs it.
"""

import os
from functools import lru_cache

from dotenv import load_dotenv

# Load .env from the backend/ folder if it exists (no-op in prod where real
# env vars are set directly, e.g. on Railway).
load_dotenv()

_TRUTHY = {"1", "true", "yes", "on"}


def _bool_env(name: str, default: bool = False) -> bool:
    """Parse a boolean env var. Anything not explicitly truthy is False."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in _TRUTHY


class Settings:
    def __init__(self) -> None:
        self.supabase_url = os.environ.get("SUPABASE_URL", "")
        self.supabase_anon_key = os.environ.get("SUPABASE_ANON_KEY", "")
        self.supabase_service_role_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
        self.supabase_jwt_secret = os.environ.get("SUPABASE_JWT_SECRET", "")

        self.roboflow_api_key = os.environ.get("ROBOFLOW_API_KEY", "")
        self.openai_api_key = os.environ.get("OPENAI_API_KEY", "")

        raw_origins = os.environ.get("CORS_ALLOW_ORIGINS", "http://localhost:5173")
        self.cors_allow_origins = [o.strip() for o in raw_origins.split(",") if o.strip()]

        # Access allowlist: only these emails may use the API.
        #
        # ⚠️ FAIL-CLOSED (2026-08-24). This used to mean "empty = open", which
        # made a forgotten env var silently expose the API to anyone who could
        # get a Supabase token — and Supabase signup is public with Google SSO,
        # so that's anyone with a Google account. An unset allowlist now DENIES
        # all requests unless open access is opted into explicitly below.
        raw_emails = os.environ.get("ALLOWED_EMAILS", "")
        self.allowed_emails = [
            e.strip().lower() for e in raw_emails.split(",") if e.strip()
        ]

        # Explicit, deliberate opt-in to true multi-tenant/open access. Only
        # honored when ALLOWED_EMAILS is empty. Must be set on purpose — the
        # whole point is that forgetting a variable can never open the door.
        self.allow_open_access = _bool_env("ALLOW_OPEN_ACCESS", False)

        # Interactive API docs (/docs, /redoc, /openapi.json). Default OFF so
        # production doesn't publish the full API surface to the internet.
        # Set ENABLE_DOCS=true locally for development.
        self.enable_docs = _bool_env("ENABLE_DOCS", False)

    def require(self, *names: str) -> None:
        """Raise a clear error if any named setting is empty."""
        missing = [n for n in names if not getattr(self, n, "")]
        if missing:
            raise RuntimeError(
                "Missing required environment variables: "
                + ", ".join(n.upper() for n in missing)
                + ". See backend/.env.example."
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()
