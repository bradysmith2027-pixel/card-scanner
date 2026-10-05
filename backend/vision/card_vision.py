"""
card_vision.py

Helpers for my Roboflow model: load it, run it on a card photo, and crop out
each field it finds.

ocr_card.py uses this. I made it its own file instead of changing
crop_card_regions.py so I didn't break the crop script that already works.
"""

DEFAULT_MODEL_ID = "bradys-workspace-wqkgm/dreamboat-slabs-3-yolov8n-t1"

# One Piece cards only have these two fields.
ONE_PIECE_ALLOWED_CLASSES = {"player_name", "card_number"}

# A little extra space around each box before cropping so text right on the
# edge doesn't get cut off.
PADDING_PIXELS = 6


def _field(obj, *names):
    """
    Get a value off a prediction whether it's an object (prediction.x) or a
    dict (prediction["x"]).
    """
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
        if isinstance(obj, dict) and name in obj:
            return obj[name]
    return None


def load_model(model_id, api_key):
    """
    Runs the model locally. Downloads the weights and runs them right here.

    This needs the inference package, which needs torch (a few GB). Works on my
    laptop but not on Railway, so use load_hosted_model() there.
    """
    from inference import get_model
    return get_model(model_id=model_id, api_key=api_key)


# --- Hosted inference ------------------------------------------------------
#
# Roboflow's API runs my same model on their servers, so it's just as accurate
# but the backend doesn't need torch.
#
# It has the same .infer(image, confidence) as the local model, so detect()
# doesn't care which one it gets. _field() already handles the dicts the API
# sends back.

HOSTED_DETECT_URL = "https://detect.roboflow.com"

# Roboflow's API won't take really big uploads, and phone photos are way bigger
# than the model needs anyway. So the photo gets shrunk first, and the boxes
# get scaled back up to the original size (see _HostedModel).
MAX_HOSTED_EDGE_PIXELS = 1600

HOSTED_TIMEOUT_SECONDS = 30


class HostedInferenceUnavailable(Exception):
    """
    Roboflow's API is up but won't run for my account.

    Usually means I'm out of credits (402). It's separate from a normal network
    error because trying again won't fix it. The API turns it into a 503 so the
    app says scanning isn't available instead of telling me to try again.
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

        # Shrink the photo for the upload and remember by how much, so the
        # boxes can be scaled back. Otherwise the boxes would be in the wrong
        # spot for any big photo.
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

        # The API wants confidence as a percent (0-100) but the local package
        # uses 0-1. If 0.25 got sent as is, it would mean 0.25%, so every
        # random box would count and the OCR would just look bad. So convert it.
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
        # 402 = out of credits (free plan is 15 a month and training uses them
        # too). 401/403 = bad key. Trying again won't fix any of these.
        if response.status_code in (401, 402, 403):
            raise HostedInferenceUnavailable(
                f"Roboflow hosted inference refused the request "
                f"(HTTP {response.status_code}). This usually means the "
                f"account is out of inference credits."
            )
        response.raise_for_status()
        results = response.json()

        # Scale the boxes back to the original photo size if it got shrunk.
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
    Run the model on an image that's already loaded (cv2). Returns a list of
    dicts: {"class_name", "confidence", "x1", "y1", "x2", "y2"}. Roboflow gives
    the center and width/height, so this turns it into box corners.
    For One Piece cards it drops any field One Piece cards don't have.
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
    If the model finds more than one box for the same field, keep the one it's
    most confident about. GPT-4o needs one crop per field, not a few.
    """
    best = {}
    for box in boxes:
        current = best.get(box["class_name"])
        if current is None or box["confidence"] > current["confidence"]:
            best[box["class_name"]] = box
    return {name: crop(image, box) for name, box in best.items()}
