"""
main.py

Starts the FastAPI app.

To run it (from backend/ with the venv on):
    uvicorn app.main:app --reload

Then check it's working:
    http://127.0.0.1:8000/health      -> {"status": "ok"}
    http://127.0.0.1:8000/docs        -> API docs

All the routers get added at the bottom.
"""

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.config import get_settings
from app.rate_limit import limiter

logger = logging.getLogger(__name__)

settings = get_settings()

# Docs are off unless ENABLE_DOCS=true. If /docs was public in production,
# anyone could see every route and field in the API.
_docs = "/docs" if settings.enable_docs else None
_redoc = "/redoc" if settings.enable_docs else None
_openapi = "/openapi.json" if settings.enable_docs else None

app = FastAPI(
    title="Dreamboat Slabs API",
    version="0.1.0",
    docs_url=_docs,
    redoc_url=_redoc,
    openapi_url=_openapi,
)

# Log who's allowed in when the app starts so I can see it in the logs.
if settings.allowed_emails:
    logger.info(
        "Access control: allowlist active (%d address(es)).",
        len(settings.allowed_emails),
    )
elif settings.allow_open_access:
    logger.warning(
        "Access control: OPEN — any signed-in Supabase user may use this API. "
        "This was opted into via ALLOW_OPEN_ACCESS=true."
    )
else:
    logger.error(
        "Access control: NOT CONFIGURED — all authenticated requests will be "
        "rejected with 403. Set ALLOWED_EMAILS (recommended) or "
        "ALLOW_OPEN_ACCESS=true."
    )

# Rate limiting for /scan. This sets up the limiter so @limiter.limit(...)
# works and returns a 429 when someone goes over.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Only my frontend can call the API from a browser. Never use "*" here since
# requests send login info.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict:
    """Quick check that the app is up. No login or database needed."""
    return {"status": "ok"}


# --- Routers ----------------------------------------------------------------
from app.routers import cards, export, lots, scan, trades  # noqa: E402

app.include_router(cards.router)
app.include_router(scan.router)
app.include_router(export.router)
app.include_router(trades.router)
app.include_router(lots.router)
