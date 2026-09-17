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

        # How detection runs: "auto" (default) | "local" | "hosted".
        #   local  — the `inference` package, weights in-process. Needs torch
        #            (~2-4 GB), so it can only ever work on the laptop. Free.
        #   hosted — Roboflow's REST endpoint, same weights, no torch. Costs
        #            credits (free tier is 15/mo shared across the account).
        #   auto   — local if `inference` imports, else hosted.
        # Anything unrecognized falls back to "auto" rather than failing a boot.
        mode = os.environ.get("ROBOFLOW_INFERENCE_MODE", "auto").strip().lower()
        self.roboflow_inference_mode = (
            mode if mode in ("auto", "local", "hosted") else "auto"
        )

        # How /scan reads a card: "fullcard" (default) | "detector".
        #
        #   fullcard — send the WHOLE card photo to GPT-4o and let it locate the
        #              fields itself. No Roboflow, no weights, no torch, no
        #              credits, no ONNX, and no AGPL question. Needs only
        #              OPENAI_API_KEY, so it is the only mode that can actually
        #              run on Railway today.
        #   detector — the original YOLO path: Roboflow detects field boxes,
        #              crops them, and GPT-4o reads the crops.
        #
        # Default flipped to "fullcard" on 2026-09-15. Every route back to
        # self-hosted weights was blocked: hosted inference is out of credits
        # (402), raw weight export needs a paid Core plan, and retraining
        # YOLOv8n lands on Ultralytics' AGPL-3.0 — rejected 2026-07-20 as
        # "risky for a commercial network app".
        #
        # ⚠️ The detector path is KEPT, not deleted. It is validated at mAP
        # 87.4% and cost real annotation time; on 2026-08-31 Claude proposed
        # removing YOLO and Brady correctly pushed back. Set
        # SCAN_VISION_MODE=detector to switch straight back.
        scan_mode = os.environ.get("SCAN_VISION_MODE", "fullcard").strip().lower()
        self.scan_vision_mode = (
            scan_mode if scan_mode in ("fullcard", "detector") else "fullcard"
        )

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

        # READ-ONLY VIEWERS (2026-09-17). Emails here may sign in and READ, but
        # every mutating request is refused at the API.
        #
        # These emails must ALSO appear in ALLOWED_EMAILS — this list restricts,
        # it does not grant. A viewer additionally needs a `viewer_grants` row
        # (migration 011) or RLS returns them an empty dashboard.
        #
        # Why this exists when RLS already blocks writes: RLS stops a viewer
        # writing to the OWNER's rows, but nothing stops them creating rows of
        # their own under their own user_id. Harmless, but it makes a "read-only"
        # account able to write, which is exactly the kind of surprise a
        # permission model should not have.
        raw_readonly = os.environ.get("READONLY_EMAILS", "")
        self.readonly_emails = [
            e.strip().lower() for e in raw_readonly.split(",") if e.strip()
        ]

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
