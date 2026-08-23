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

        # Access allowlist: only these emails may use the API. Empty = open
        # (multi-tenant). Set to lock the app to specific owner(s) pre-launch.
        raw_emails = os.environ.get("ALLOWED_EMAILS", "")
        self.allowed_emails = [
            e.strip().lower() for e in raw_emails.split(",") if e.strip()
        ]

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
