"""
scan_service.py — the in-memory scan pipeline behind POST /scan.

Reuses the already-working, live-tested logic from the project-root scripts
(card_vision.py + ocr_card.py) rather than reimplementing it:
  - card_vision.detect / best_crop_per_class  — YOLO v3 region detection + crop
  - ocr_card.build_messages / build_schema / merge_field / guess_card_type_from_logo
    / FIELDS_BY_CARD_TYPE                       — GPT-4o OCR + front/back merge

The ONE difference from the CLI: images arrive as uploaded bytes, so we decode
them in memory (cv2.imdecode) instead of reading file paths. Decoding to an
array and re-encoding crops as JPEG also strips EXIF (incl. GPS) for free.

The Roboflow model and OpenAI client are cached (loaded once), so only the
first scan pays the model-download cost.
"""

import json
import pathlib
import sys
from functools import lru_cache

import numpy as np

from app.config import get_settings

# card_vision.py / ocr_card.py live at the project root, one level above backend/.
_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_CONFIDENCE = 0.25


class ScanError(Exception):
    """Raised for user-actionable scan failures (bad card type, no detections)."""


class ScanUnavailable(Exception):
    """
    Raised when the vision dependencies aren't installed.

    The lightweight deployment ships without `inference`/opencv, so every
    endpoint except /scan works there. The router maps this to 503.
    """


@lru_cache(maxsize=1)
def _vision():
    """
    Import card_vision / ocr_card on first use rather than at module import.

    card_vision pulls in `inference` (torch + opencv, ~2-4 GB). Importing it
    at module level meant main.py -> routers/scan.py -> scan_service.py could
    not boot at all without those deps installed. Deferring it lets one
    codebase run both ways: with the CV deps present /scan works normally;
    without them the rest of the API boots fine and only /scan returns 503.
    Mirrors the deferral already used for cv2 (_decode) and OpenAI (_openai).
    """
    try:
        import card_vision
        import ocr_card
    except ImportError as e:  # pragma: no cover - depends on deploy target
        raise ScanUnavailable(
            "Card scanning isn't available on this deployment - "
            "vision dependencies are not installed."
        ) from e

    return card_vision, ocr_card


@lru_cache(maxsize=1)
def _model():
    card_vision, _ = _vision()
    settings = get_settings()
    settings.require("roboflow_api_key")
    return card_vision.load_model(card_vision.DEFAULT_MODEL_ID, settings.roboflow_api_key)


@lru_cache(maxsize=1)
def _openai():
    from openai import OpenAI

    settings = get_settings()
    settings.require("openai_api_key")
    return OpenAI(api_key=settings.openai_api_key)


def _decode(image_bytes: bytes):
    import cv2

    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    image = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if image is None:
        raise ScanError("Uploaded file is not a decodable image.")
    return image


def _crops(model, image, card_type):
    card_vision, _ = _vision()
    boxes = card_vision.detect(model, image, confidence=_CONFIDENCE, card_type=card_type)
    return card_vision.best_crop_per_class(image, boxes)


def run_scan(
    front_bytes: bytes,
    back_bytes: bytes | None,
    capture_mode: str,
    card_type_override: str | None,
) -> dict:
    """
    Detect + crop + GPT-4o OCR one card, merging front/back. Returns the
    identified fields for the confirmation screen — does NOT persist anything.
    """
    _, ocr_card = _vision()

    if capture_mode not in ("sports", "tcg"):
        raise ScanError("capture_mode must be 'sports' or 'tcg'.")

    front_img = _decode(front_bytes)

    # Resolve card_type (in-memory version of ocr_card.resolve_card_type, minus
    # argparse/sys.exit).
    if capture_mode == "tcg":
        card_type, source = "one_piece", "fixed_tcg"
        back_bytes = None  # TCG (One Piece) is front-only per project spec.
    else:  # sports
        if card_type_override in ("topps", "panini"):
            card_type, source = card_type_override, "user_override"
        elif card_type_override in (None, ""):
            card_type, source = None, "logo_guess"
        else:
            raise ScanError("card_type for sports must be 'topps', 'panini', or omitted.")

    model = _model()
    client = _openai()

    front_crops = _crops(model, front_img, card_type)

    if card_type is None:
        # Sports with no override — guess topps vs panini from the set_logo crop.
        card_type = ocr_card.guess_card_type_from_logo(client, front_crops.get("set_logo"))
        if card_type is None:
            raise ScanError(
                "Couldn't determine card type from the logo — resend with "
                "card_type set to 'topps' or 'panini'."
            )
        # Re-crop the front now that we know the type (no-op for topps/panini,
        # which don't filter classes, but keeps behavior explicit).
        front_crops = _crops(model, front_img, card_type)

    back_crops = None
    if capture_mode == "sports" and back_bytes:
        back_crops = _crops(model, _decode(back_bytes), card_type)

    messages = ocr_card.build_messages(front_crops, back_crops, card_type)
    schema = ocr_card.build_schema(card_type)

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        response_format={"type": "json_schema", "json_schema": schema},
    )
    raw = json.loads(response.choices[0].message.content)

    fields = ocr_card.FIELDS_BY_CARD_TYPE[card_type]
    result: dict = {"card_type": card_type, "card_type_source": source}
    needs_review: list[str] = []
    conflicts: dict = {}
    for field in fields:
        value, conflict = ocr_card.merge_field(
            raw.get("front", {}).get(field), raw.get("back", {}).get(field)
        )
        result[field] = value
        if conflict:
            needs_review.append(field)
            conflicts[field] = conflict

    result["needs_review"] = needs_review
    result["conflicts"] = conflicts
    return result
