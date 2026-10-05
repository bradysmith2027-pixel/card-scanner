"""
scan_service.py

The scan pipeline behind POST /scan.

It reuses the code from vision/ (card_vision.py and ocr_card.py) instead of
rewriting it:
  - card_vision.detect / best_crop_per_class   YOLO finds each field and crops it
  - ocr_card.build_messages / build_schema / merge_field / guess_card_type_from_logo
    / FIELDS_BY_CARD_TYPE                       GPT-4o reads it and merges front/back

The only difference from running it on the command line is the images come in
as uploaded bytes, so they get decoded in memory instead of read from a file.
Re-saving the crops as JPEG also strips the photo's metadata (like GPS) for
free.

The Roboflow model and OpenAI client only get loaded once, so only the first
scan is slow.

There are two modes (SCAN_VISION_MODE in config.py):
  "fullcard" (default): the whole photo goes to GPT-4o and it finds the fields
      itself. Only needs OPENAI_API_KEY, so it's what runs in production.
  "detector": the original YOLO way. Still here and still works.

I switched the default because the detector needs Roboflow credits or the
model weights, and I can't get either on the free plan. The detector code is
still here since the model is good (87.4% mAP). One env var switches back.
"""

import json
from functools import lru_cache

import numpy as np

from app.config import get_settings

_CONFIDENCE = 0.25


class ScanError(Exception):
    """Error for scan problems the user can fix (wrong card type, nothing found)."""


class ScanUnavailable(Exception):
    """
    Error for when the vision packages aren't installed.

    The server runs without inference/opencv, so everything works except
    /scan. The router turns this into a 503.
    """


@lru_cache(maxsize=1)
def _vision():
    """
    Import the vision code the first time a scan happens instead of at startup.

    It lives in backend/vision/. It used to be in the project root, which
    Railway doesn't deploy, so /scan never worked in production.

    cv2 and openai are only needed for scanning, so importing them late means
    the rest of the API still starts up even if they're missing.
    """
    try:
        from vision import card_vision, ocr_card
    except ImportError as e:  # pragma: no cover - depends on where it's deployed
        raise ScanUnavailable(
            "Card scanning isn't available on this deployment - "
            "vision dependencies are not installed."
        ) from e

    return card_vision, ocr_card


@lru_cache(maxsize=1)
def _model():
    """
    Load the detection model, local or hosted, based on ROBOFLOW_INFERENCE_MODE.

    "auto" (default) uses the local inference package if it's installed and
    Roboflow's API if it's not. My laptop runs the model for free, and Railway
    doesn't have torch so it uses the API. Same code either way, and scanning a
    bunch of cards on my laptop doesn't use up any credits.
    """
    card_vision, _ = _vision()
    settings = get_settings()
    settings.require("roboflow_api_key")

    mode = settings.roboflow_inference_mode
    if mode == "auto":
        try:
            import inference  # noqa: F401
            mode = "local"
        except ImportError:
            mode = "hosted"

    if mode == "local":
        return card_vision.load_model(
            card_vision.DEFAULT_MODEL_ID, settings.roboflow_api_key
        )
    return card_vision.load_hosted_model(
        card_vision.DEFAULT_MODEL_ID, settings.roboflow_api_key
    )


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
    try:
        boxes = card_vision.detect(
            model, image, confidence=_CONFIDENCE, card_type=card_type
        )
    except card_vision.HostedInferenceUnavailable as e:
        # Out of Roboflow credits. This is a 503 (not available) instead of a
        # 502 because trying again won't help. Scanning has to happen on the
        # laptop.
        raise ScanUnavailable(str(e)) from e
    return card_vision.best_crop_per_class(image, boxes)


# Answers that just mean "no parallel". The prompt tells GPT not to use these,
# but if it does anyway, I don't want "Base" saved as the parallel. A blank
# field is better than a wrong label.
_NOT_A_PARALLEL = {
    "base",
    "base card",
    "none",
    "n/a",
    "na",
    "null",
    "unknown",
    "standard",
    "regular",
    "no parallel",
}


def run_scan(
    front_bytes: bytes,
    back_bytes: bytes | None,
    capture_mode: str,
    card_type_override: str | None,
) -> dict:
    """
    Read one card (front and back merged together) and return what it found
    for the confirmation screen. Doesn't save anything.
    """
    _, ocr_card = _vision()

    if capture_mode not in ("sports", "tcg"):
        raise ScanError("capture_mode must be 'sports' or 'tcg'.")

    front_img = _decode(front_bytes)

    # Figure out card_type. Same as ocr_card.resolve_card_type but without the
    # command line stuff.
    if capture_mode == "tcg":
        card_type, source = "one_piece", "fixed_tcg"
        back_bytes = None  # One Piece only needs the front.
    else:  # sports
        if card_type_override in ("topps", "panini"):
            card_type, source = card_type_override, "user_override"
        elif card_type_override in (None, ""):
            card_type, source = None, "logo_guess"
        else:
            raise ScanError("card_type for sports must be 'topps', 'panini', or omitted.")

    client = _openai()
    vision_mode = get_settings().scan_vision_mode

    back_img = (
        _decode(back_bytes) if capture_mode == "sports" and back_bytes else None
    )

    if vision_mode == "fullcard":
        # No detector. The whole card goes to GPT-4o and it finds the fields.
        # Doesn't touch Roboflow at all, so this is what works in production.
        if card_type is None:
            card_type = ocr_card.guess_card_type_from_card(client, front_img)
            if card_type is None:
                raise ScanError(
                    "Couldn't determine card type from the photo — resend with "
                    "card_type set to 'topps' or 'panini'."
                )
        messages = ocr_card.build_fullcard_messages(front_img, back_img, card_type)
    else:
        model = _model()
        front_crops = _crops(model, front_img, card_type)

        if card_type is None:
            # Sports card with no brand picked, so guess Topps vs Panini from the logo.
            card_type = ocr_card.guess_card_type_from_logo(
                client, front_crops.get("set_logo")
            )
            if card_type is None:
                raise ScanError(
                    "Couldn't determine card type from the logo — resend with "
                    "card_type set to 'topps' or 'panini'."
                )
            # Crop the front again now that the type is known. Doesn't change
            # anything for Topps/Panini, but it keeps it clear.
            front_crops = _crops(model, front_img, card_type)

        back_crops = _crops(model, back_img, card_type) if back_img is not None else None
        messages = ocr_card.build_messages(front_crops, back_crops, card_type)

    schema = ocr_card.build_schema(card_type)

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        response_format={"type": "json_schema", "json_schema": schema},
    )
    raw = json.loads(response.choices[0].message.content)

    fields = ocr_card.FIELDS_BY_CARD_TYPE[card_type]
    result: dict = {
        "card_type": card_type,
        "card_type_source": source,
        # Which mode read the card. The two modes make different kinds of
        # mistakes, so it helps to know which one did it.
        "vision_mode": vision_mode,
    }
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

    # --- category: just a suggestion -------------------------------------
    #
    # Checked against the allowed list instead of trusted, same as the brand
    # guess. The category column doesn't have a CHECK constraint, so a weird
    # value would save fine and then mess up every report that groups by
    # category.
    #
    # Anything not on the list becomes None, and the confirm screen makes me
    # pick one from the dropdown like normal.
    raw_category = raw.get("category")
    category = raw_category.strip().lower() if isinstance(raw_category, str) else None
    result["category"] = (
        category if category in ocr_card.ALLOWED_CATEGORIES else None
    )

    # --- parallel: GPT can guess it, but has to say how sure it is --------
    #
    # I used to always pick the parallel by hand. That made sense with the
    # detector since it only sent crops of the text, and you can't tell the
    # finish from a crop of the card number. Full card mode can see the whole
    # card, so now it can take a guess.
    #
    # A wrong parallel is a big deal though. Base vs Silver Prizm can be a 10x
    # price difference, and a wrong one saved with confidence is worse than a
    # blank because nothing will question it later.
    #
    # So parallel_confidence is "high" only if the parallel name is actually
    # printed on the card. "low" means it guessed from how the card looks.
    # Anything that isn't "high" gets flagged and shows up amber.
    raw_parallel = raw.get("parallel")
    parallel = raw_parallel.strip() if isinstance(raw_parallel, str) else None
    # Catch it if GPT describes the card instead of naming the parallel, or
    # answers "Base"/"None" when it isn't supposed to.
    if parallel and (len(parallel) > 60 or parallel.lower() in _NOT_A_PARALLEL):
        parallel = None
    result["parallel"] = parallel or None

    raw_conf = raw.get("parallel_confidence")
    confidence = raw_conf.strip().lower() if isinstance(raw_conf, str) else None
    confidence = confidence if confidence in ("high", "low") else None
    result["parallel_confidence"] = confidence if result["parallel"] else None

    # Flag it unless it was actually read off the card. If the confidence is
    # missing or weird, it gets flagged too.
    if result["parallel"] and result["parallel_confidence"] != "high":
        needs_review.append("parallel")

    # Check it against the serial too. A numbered card has to be some kind of
    # parallel since base cards aren't numbered. So if it's numbered and no
    # parallel was found, flag it so I fill it in.
    if result.get("serial") and not result["parallel"]:
        needs_review.append("parallel")

    result["needs_review"] = needs_review
    result["conflicts"] = conflicts
    return result
