"""
scan.py — POST /scan: identify a card from photo(s).

Security posture (per the schema design doc — this is the cost-abuse surface):
  - Auth required (current_user).
  - Per-user rate limit: 10/min (slowapi) — matches "scan front then back, one
    card at a time" and catches a runaway loop fast.
  - File validation BEFORE any model/GPT-4o work: content-type must be an image,
    size capped, and the bytes must actually decode as an image.
  - Returns identified fields only — does NOT persist. Saving happens after the
    user confirms on the (frontend) confirmation screen.
"""

from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from starlette.concurrency import run_in_threadpool

from app.auth import AuthedUser, current_user
from app.rate_limit import limiter
from app.scan_service import ScanError, ScanUnavailable, run_scan

router = APIRouter(tags=["scan"])

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB per image


async def _read_valid_image(upload: UploadFile, label: str) -> bytes:
    if upload.content_type is None or not upload.content_type.startswith("image/"):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"{label} must be an image.",
        )
    data = await upload.read()
    if not data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"{label} is empty."
        )
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"{label} exceeds the 10 MB limit.",
        )
    return data


@router.post("/scan")
@limiter.limit("10/minute")
async def scan_card(
    request: Request,  # required by slowapi's limiter
    front: UploadFile = File(...),
    back: Optional[UploadFile] = File(None),
    capture_mode: str = Form(...),
    card_type: Optional[str] = Form(None),
    user: AuthedUser = Depends(current_user),
) -> dict:
    if capture_mode not in ("sports", "tcg"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="capture_mode must be 'sports' or 'tcg'.",
        )

    front_bytes = await _read_valid_image(front, "front")
    back_bytes = None
    if back is not None:
        back_bytes = await _read_valid_image(back, "back")

    try:
        # Blocking (model + GPT-4o) — run off the event loop.
        result = await run_in_threadpool(
            run_scan, front_bytes, back_bytes, capture_mode, card_type
        )
    except ScanError as e:
        # User-actionable: bad card type, undecodable image, no detections.
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    except ScanUnavailable as e:
        # Lightweight deployment without the vision deps: the rest of the API
        # works, scanning does not. Distinct from a transient 502 so the
        # frontend can say "scan locally" rather than "try again".
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e)
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Scan failed. Please try again.",
        )

    return result
