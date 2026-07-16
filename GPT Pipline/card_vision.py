"""
card_vision.py

Shared helpers for talking to the trained Roboflow model: load it, run
detection on a card photo, and crop out each detected field.

Used by ocr_card.py (the real OCR step). Kept as its own file rather than
refactoring crop_card_regions.py to share it, so the already-working crop
script stays untouched.
"""

DEFAULT_MODEL_ID = "bradys-workspace-wqkgm/dreamboat-slabs-1-yolov8n-t1"

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
    from inference import get_model
    return get_model(model_id=model_id, api_key=api_key)


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
