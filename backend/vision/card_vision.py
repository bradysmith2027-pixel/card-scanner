"""
card_vision.py

Shared helpers for talking to the trained Roboflow model: load it, run
detection on a card photo, and crop out each detected field.

Used by ocr_card.py (the real OCR step). Kept as its own file rather than
refactoring crop_card_regions.py to share it, so the already-working crop
script stays untouched.
"""

DEFAULT_MODEL_ID = "bradys-workspace-wqkgm/dreamboat-slabs-3-yolov8n-t1"

# One-piece cards only ever get these two fields per the project spec.
ONE_PIECE_ALLOWED_CLASSES = {"player_name", "card_number"}

# Small buffer added around every detected box before cropping, so text
# right at the edge of a box doesn't get sliced off.
PADDING_PIXELS = 6


def _field(obj, *names):
    """
    Pull a field off a prediction whether it comes back as an object with
    attributes (e.g. prediction.x) or a plain dict (e.g. prediction["x"]).
    """
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
        if isinstance(obj, dict) and name in obj:
            return obj[name]
    return None


def load_model(model_id, api_key):
    """
    Local inference: downloads the weights and runs them in-process.

    Pulls in `inference`, which drags torch (~2-4 GB). Fine on Brady's laptop,
    impossible on Railway — use load_hosted_model() there.
    """
    from inference import get_model
    return get_model(model_id=model_id, api_key=api_key)


# --- Hosted inference ------------------------------------------------------
#
# Roboflow's REST endpoint runs the SAME trained weights on their servers, so
# accuracy is unchanged (v3, mAP 87.4%) but the backend needs no torch. This is
# what makes /scan deployable.
#
# The returned object exposes .infer(image, confidence) so it is a drop-in for
# the local model above — detect() below does not care which one it got, and
# _field() already reads the dict-shaped predictions the REST API returns.

HOSTED_DETECT_URL = "https://detect.roboflow.com"

# Roboflow's hosted endpoint rejects very large uploads, and a phone photo is
# far bigger than the model's input anyway. Longest side is capped before
# encoding; boxes come back in the ORIGINAL pixel space (see _HostedModel).
MAX_HOSTED_EDGE_PIXELS = 1600

HOSTED_TIMEOUT_SECONDS = 30


class HostedInferenceUnavailable(Exception):
    """
    The hosted endpoint is reachable but refuses to serve this account.

    Almost always exhausted credits (HTTP 402). Kept separate from a generic
    network error because it is not transient and retrying cannot fix it — the
    API layer turns this into a 503 "scan isn't available here" rather than a
    502 "try again", so the UI doesn't tell Brady to retry forever.
    """


class _HostedModel:
    def __init__(self, model_id, api_key):
        self.model_id = model_id
        self.api_key = api_key

    def infer(self, image, confidence=0.25):
        import base64

        import cv2
        import httpx

        original_h, original_w = image.shape[:2]

        # Downscale for the upload, remembering the factor so the boxes can be
        # mapped back. Without this, every coordinate would be silently wrong
        # whenever a photo exceeded the cap.
        scale = min(1.0, MAX_HOSTED_EDGE_PIXELS / max(original_h, original_w))
        if scale < 1.0:
            send = cv2.resize(
                image,
                (int(original_w * scale), int(original_h * scale)),
                interpolation=cv2.INTER_AREA,
            )
        else:
            send = image

        ok, buffer = cv2.imencode(".jpg", send)
        if not ok:
            raise RuntimeError("Could not JPEG-encode the image for hosted inference.")

        # 🔴 The hosted REST API takes confidence as a PERCENT (0-100); the
        # local `inference` package takes a fraction (0-1). Passing 0.25
        # straight through would mean a 0.25% threshold, so every speculative
        # box fires and best_crop_per_class picks noise -- which would look
        # like bad OCR, not like a bug. Convert explicitly.
        confidence_percent = confidence * 100 if confidence <= 1 else confidence

        response = httpx.post(
            f"{HOSTED_DETECT_URL}/{self.model_id}",
            params={
                "api_key": self.api_key,
                "confidence": confidence_percent,
                "format": "json",
            },
            content=base64.b64encode(buffer.tobytes()),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=HOSTED_TIMEOUT_SECONDS,
        )
        # 402 = out of hosted-inference credits (the free tier is 15/month for
        # the whole account, and training consumes them too). 401/403 = key
        # rejected. None of these are retryable.
        if response.status_code in (401, 402, 403):
            raise HostedInferenceUnavailable(
                f"Roboflow hosted inference refused the request "
                f"(HTTP {response.status_code}). This usually means the "
                f"account is out of inference credits."
            )
        response.raise_for_status()
        results = response.json()

        # Map coordinates back to the original image if we shrank it.
        if scale < 1.0:
            for pred in results.get("predictions", []):
                for key in ("x", "y", "width", "height"):
                    if key in pred and pred[key] is not None:
                        pred[key] = pred[key] / scale

        return results


def load_hosted_model(model_id, api_key):
    return _HostedModel(model_id, api_key)


def get_predictions(results):
    predictions = _field(results, "predictions")
    if predictions is None:
        predictions = results.get("predictions", []) if isinstance(results, dict) else []
    return predictions


def detect(model, image, confidence=0.25, card_type=None):
    """
    Run detection on an already-loaded (BGR/cv2) image. Returns a list of
    dicts: {"class_name", "confidence", "x1", "y1", "x2", "y2"} (already
    converted from Roboflow's center x/y/width/height to a bounding box).
    If card_type == "one_piece", filters out any class One Piece cards
    aren't supposed to have (per the project spec).
    """
    img_height, img_width = image.shape[:2]
    raw_results = model.infer(image, confidence=confidence)
    results = raw_results[0] if isinstance(raw_results, list) else raw_results
    predictions = get_predictions(results)

    boxes = []
    for pred in predictions:
        class_name = _field(pred, "class_name", "class")
        conf = _field(pred, "confidence")
        cx, cy = _field(pred, "x"), _field(pred, "y")
        w, h = _field(pred, "width"), _field(pred, "height")
        if None in (class_name, conf, cx, cy, w, h):
            continue

        class_name = str(class_name)
        if card_type == "one_piece" and class_name not in ONE_PIECE_ALLOWED_CLASSES:
            continue

        x1 = max(0, int(cx - w / 2) - PADDING_PIXELS)
        y1 = max(0, int(cy - h / 2) - PADDING_PIXELS)
        x2 = min(img_width, int(cx + w / 2) + PADDING_PIXELS)
        y2 = min(img_height, int(cy + h / 2) + PADDING_PIXELS)
        boxes.append({
            "class_name": class_name,
            "confidence": float(conf),
            "x1": x1, "y1": y1, "x2": x2, "y2": y2,
        })
    return boxes


def crop(image, box):
    return image[box["y1"]:box["y2"], box["x1"]:box["x2"]]


def best_crop_per_class(image, boxes):
    """
    If the model fires more than one box for the same field on one photo,
    keep only the highest-confidence one -- OCR needs a single crop per
    field to send to GPT-4o, not several competing ones.
    """
    best = {}
    for box in boxes:
        current = best.get(box["class_name"])
        if current is None or box["confidence"] > current["confidence"]:
            best[box["class_name"]] = box
    return {name: crop(image, box) for name, box in best.items()}
