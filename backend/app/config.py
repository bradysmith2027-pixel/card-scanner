"""
config.py

Loads all the settings from environment variables (and a local .env file if
there is one) when the app starts.
"""

import os
from functools import lru_cache

from dotenv import load_dotenv

# Load .env from backend/ if it's there. On Railway the variables are set in the
# dashboard so this just does nothing.
load_dotenv()

_TRUTHY = {"1", "true", "yes", "on"}


def _bool_env(name: str, default: bool = False) -> bool:
    """Read a true/false env var. Anything that isn't clearly true counts as False."""
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

        # How the YOLO detector runs: "auto" (default), "local", or "hosted".
        #   local:  runs the model on my laptop with the inference package. Free,
        #           but needs torch (a few GB) so it can't run on Railway.
        #   hosted: calls Roboflow's API instead. Same model, no torch, but it
        #           uses Roboflow credits.
        #   auto:   local if the inference package is installed, otherwise hosted.
        # If it's set to something else, it just uses auto.
        mode = os.environ.get("ROBOFLOW_INFERENCE_MODE", "auto").strip().lower()
        self.roboflow_inference_mode = (
            mode if mode in ("auto", "local", "hosted") else "auto"
        )

        # How /scan reads a card: "fullcard" (default) or "detector".
        #
        #   fullcard: sends the whole card photo to GPT-4o and lets it find the
        #             fields. Only needs OPENAI_API_KEY, so this is what runs on
        #             Railway.
        #   detector: the original way. My YOLO model finds each field, crops
        #             it, and GPT-4o reads the crops.
        #
        # I switched the default to fullcard because the detector needs
        # Roboflow credits or the model weights, and I don't have either on the
        # free plan. I kept the detector code since the model works well (87.4%
        # mAP). Set SCAN_VISION_MODE=detector to go back to it.
        scan_mode = os.environ.get("SCAN_VISION_MODE", "fullcard").strip().lower()
        self.scan_vision_mode = (
            scan_mode if scan_mode in ("fullcard", "detector") else "fullcard"
        )

        raw_origins = os.environ.get("CORS_ALLOW_ORIGINS", "http://localhost:5173")
        self.cors_allow_origins = [o.strip() for o in raw_origins.split(",") if o.strip()]

        # Only these emails can use the API.
        #
        # If this is empty, everyone gets blocked. It used to be the other way
        # around (empty meant anyone could get in), and since anyone with a
        # Google account can sign up, forgetting to set it would have left the
        # API open.
        raw_emails = os.environ.get("ALLOWED_EMAILS", "")
        self.allowed_emails = [
            e.strip().lower() for e in raw_emails.split(",") if e.strip()
        ]

        # Turns on open access for everyone. Only works when ALLOWED_EMAILS is
        # empty, and it has to be set on purpose.
        self.allow_open_access = _bool_env("ALLOW_OPEN_ACCESS", False)

        # Read-only accounts. These people can sign in and look at everything
        # but can't change anything.
        #
        # They also have to be in ALLOWED_EMAILS, and they need a row in
        # viewer_grants (migration 011) or they'll just see an empty dashboard.
        #
        # RLS already stops them from editing my cards, but without this they
        # could still add cards of their own. Read-only should mean read-only.
        raw_readonly = os.environ.get("READONLY_EMAILS", "")
        self.readonly_emails = [
            e.strip().lower() for e in raw_readonly.split(",") if e.strip()
        ]

        # The API docs pages (/docs, /redoc, /openapi.json). Off by default so
        # they aren't public in production. I turn them on locally.
        self.enable_docs = _bool_env("ENABLE_DOCS", False)

    def require(self, *names: str) -> None:
        """Throw a clear error if any of these settings are missing."""
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
