"""
main.py — FastAPI app entry point.

Boot it (from the backend/ folder, venv active):
    uvicorn app.main:app --reload

Then check it's alive:
    http://127.0.0.1:8000/health      -> {"status": "ok"}
    http://127.0.0.1:8000/docs        -> interactive API docs

Routers (scan, cards, export) get mounted here as they're built.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.config import get_settings
from app.rate_limit import limiter

app = FastAPI(title="Dreamboat Slabs API", version="0.1.0")

settings = get_settings()

# Rate limiting (used by the scan endpoint). Register the limiter + the 429
# handler so @limiter.limit(...) works and over-limit requests return 429.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS locked to the configured frontend origin(s) — never "*", which would
# be a data-leak combo with credentialed requests.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict:
    """Liveness check — no auth, no DB. Confirms the app booted."""
    return {"status": "ok"}


# --- Routers (mounted as they're built) ------------------------------------
from app.routers import cards, export, scan  # noqa: E402

app.include_router(cards.router)
app.include_router(scan.router)
app.include_router(export.router)
