"""
crop_card_regions.py

WHAT THIS SCRIPT DOES (plain English):
  You give it a card photo (or a folder of card photos). It asks your
  trained model to find the boxes on the card (year, set name, set logo,
  card number, player name) and saves each box as its own small picture.

  This is a "check before you build" step. Before we hook this up to GPT-4o
  to read the text, we want to LOOK at the cropped pictures ourselves and
  confirm they're clean and readable. If a crop is blurry, cut off, or
  includes the wrong area, GPT-4o won't be able to read it either -- better
  to catch that now.

WHY THIS VERSION IS DIFFERENT FROM A "NORMAL" YOLO SCRIPT:
  Roboflow's Public (free) plan does not allow manually exporting the raw
  .pt weights file. Instead, this script uses Roboflow's free "inference"
  package, which loads your model by its model ID (using your API key) and
  downloads/caches the weights automatically behind the scenes the first
  time it runs. After that first run, it works offline. This is still
  "self-hosted" -- it runs on your machine, not billed per call -- it just
  doesn't hand you a loose .pt file to manage yourself. This same
  model-loading approach can be dropped directly into the FastAPI backend
  later (Step 5) with no separate server needed.

HOW TO RUN IT (see README.md for full plain-English instructions):

  One photo:
    python crop_card_regions.py --image test_card.jpg

  A whole folder of photos:
    python crop_card_regions.py --images-dir ./test_photos

  Output goes to a new "crops_output" folder by default. Each cropped
  image is named so you know which photo and which field it came from:
    crops_output/crops/player_name/test_card_player_name_0.jpg

  It also saves a copy of the original photo with boxes drawn on it
  (in an "annotated" folder) so you can see what the model detected at a
  glance, without opening every single crop.
"""

import argparse
import os
import sys
from pathlib import Path

# Your trained model, as shown in the Roboflow dashboard.
DEFAULT_MODEL_ID = "bradys-workspace-wqkgm/dreamboat-slabs-1-yolov8n-t1"

# One-piece cards only ever get these two fields per the project spec.
ONE_PIECE_ALLOWED_CLASSES = {"player_name", "card_number"}

# Small buffer added around every detected box before cropping, so text
# right at the edge of a box doesn't get sliced off. Tweak if crops look
# too tight or too loose.
PADDING_PIXELS = 6

# Colors (BGR) used to draw boxes on the annotated preview image.
BOX_COLOR = (60, 200, 60)
TEXT_COLOR = (255, 255, 255)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Crop model-detected card regions into individual images for review."
    )
    parser.add_argument(
        "--model-id",
        default=DEFAULT_MODEL_ID,
        help=f"Roboflow model ID to run (default: {DEFAULT_MODEL_ID}).",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help=(
            "Your Roboflow API key. If omitted, reads the ROBOFLOW_API_KEY "
            "environment variable instead (recommended, so your key isn't "
            "typed into your terminal history)."
        ),
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--image", help="Path to a single card photo.")
    source.add_argument(
        "--images-dir", help="Path to a folder containing multiple card photos."
    )
    parser.add_argument(
        "--output",
        default="crops_output",
        help="Where to save the cropped images and annotated previews (default: crops_output).",
    )
    parser.add_argument(
        "--card-type",
        choices=["topps", "panini", "one_piece"],
        default=None,
        help=(
            "Optional. If set to 'one_piece', only player_name and card_number "
            "crops are saved (matches the locked-in project spec). If omitted, "
            "all detected regions are saved for any card type."
        ),
    )
    parser.add_argument(
        "--confidence",
        type=float,
        default=0.25,
        help="Minimum detection confidence to keep a box, from 0 to 1 (default: 0.25). Lower this if the model is missing obvious fields; raise it if you're getting junk boxes.",
    )
    return parser.parse_args()


def get_image_paths(args):
    if args.image:
        return [Path(args.image)]
    folder = Path(args.images_dir)
    valid_ext = {".jpg", ".jpeg", ".png", ".bmp"}
    images = sorted(p for p in folder.iterdir() if p.suffix.lower() in valid_ext)
    if not images:
        print(f"No image files found in {folder}")
        sys.exit(1)
    return images


def _field(obj, *names):
    """
    Pull a field off a prediction whether it comes back as an object with
    attributes (e.g. prediction.x) or a plain dict (e.g. prediction["x"]).
    Tries each name in `names` in order and returns the first one found.
    Different versions of Roboflow's `inference` package have returned
    predictions both ways, so this keeps the script working either way.
    """
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
        if isinstance(obj, dict) and name in obj:
            return obj[name]
    return None


def get_predictions(results):
    """
    Normalize the output of model.infer(image)[0] into a plain list of
    prediction objects/dicts, regardless of exact SDK version shape.
    """
    predictions = _field(results, "predictions")
    if predictions is None:
        # Some versions return the dict itself with no wrapping object.
        predictions = results.get("predictions", []) if isinstance(results, dict) else []
    return predictions


def draw_annotated_preview(image, predictions):
    import cv2

    annotated = image.copy()
    for pred in predictions:
        cx, cy = _field(pred, "x"), _field(pred, "y")
        w, h = _field(pred, "width"), _field(pred, "height")
        class_name = _field(pred, "class_name", "class")
        if None in (cx, cy, w, h):
            continue
        x1, y1 = int(cx - w / 2), int(cy - h / 2)
        x2, y2 = int(cx + w / 2), int(cy + h / 2)
        cv2.rectangle(annotated, (x1, y1), (x2, y2), BOX_COLOR, 2)
        cv2.putText(
            annotated,
            str(class_name),
            (x1, max(0, y1 - 6)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            TEXT_COLOR,
            1,
            cv2.LINE_AA,
        )
    return annotated


def main():
    args = parse_args()

    api_key = args.api_key or os.environ.get("ROBOFLOW_API_KEY")
    if not api_key:
        print(
            "No Roboflow API key found.\n"
            "Either pass --api-key YOUR_KEY, or set it as an environment variable first:\n\n"
            "    export ROBOFLOW_API_KEY=your_key_here      (Mac/Linux)\n"
            "    setx ROBOFLOW_API_KEY your_key_here         (Windows)\n\n"
            "Find your key in Roboflow under Settings -> API Keys.\n"
        )
        sys.exit(1)

    try:
        from inference import get_model
    except ImportError:
        print(
            "The 'inference' package isn't installed.\n"
            "Install it first with:\n\n"
            "    pip install inference\n"
        )
        sys.exit(1)

    import cv2

    image_paths = get_image_paths(args)

    output_dir = Path(args.output)
    crops_dir = output_dir / "crops"
    annotated_dir = output_dir / "annotated"
    crops_dir.mkdir(parents=True, exist_ok=True)
    annotated_dir.mkdir(parents=True, exist_ok=True)

    print(f"Loading model {args.model_id} ...")
    print("(First run downloads and caches the model -- this can take a minute.)")
    model = get_model(model_id=args.model_id, api_key=api_key)

    total_crops = 0
    summary = []  # (image_name, class_name, confidence)

    for image_path in image_paths:
        print(f"\nProcessing {image_path.name} ...")
        image = cv2.imread(str(image_path))
        if image is None:
            print(f"  Could not read image, skipping: {image_path}")
            continue

        img_height, img_width = image.shape[:2]

        raw_results = model.infer(image, confidence=args.confidence)
        results = raw_results[0] if isinstance(raw_results, list) else raw_results
        predictions = get_predictions(results)

        if not predictions:
            print("  No fields detected. Try lowering --confidence or check the photo quality.")
            continue

        # Save an annotated preview (original photo with boxes drawn on it)
        # so it's easy to eyeball detections without opening every crop.
        annotated_image = draw_annotated_preview(image, predictions)
        annotated_path = annotated_dir / f"{image_path.stem}_annotated.jpg"
        cv2.imwrite(str(annotated_path), annotated_image)

        # Per-image counter so repeated detections of the same field
        # (e.g. two boxes both called "card_number") get unique filenames.
        seen_counts = {}

        for pred in predictions:
            class_name = _field(pred, "class_name", "class")
            confidence = _field(pred, "confidence")
            cx, cy = _field(pred, "x"), _field(pred, "y")
            w, h = _field(pred, "width"), _field(pred, "height")

            if None in (class_name, confidence, cx, cy, w, h):
                print(f"  Skipping a prediction with unexpected shape: {pred}")
                continue

            if args.card_type == "one_piece" and class_name not in ONE_PIECE_ALLOWED_CLASSES:
                continue

            # x/y from Roboflow are the CENTER of the box, not a corner --
            # convert to a top-left/bottom-right box before cropping.
            x1 = max(0, int(cx - w / 2) - PADDING_PIXELS)
            y1 = max(0, int(cy - h / 2) - PADDING_PIXELS)
            x2 = min(img_width, int(cx + w / 2) + PADDING_PIXELS)
            y2 = min(img_height, int(cy + h / 2) + PADDING_PIXELS)

            crop = image[y1:y2, x1:x2]

            class_dir = crops_dir / str(class_name)
            class_dir.mkdir(parents=True, exist_ok=True)

            idx = seen_counts.get(class_name, 0)
            seen_counts[class_name] = idx + 1
            crop_filename = f"{image_path.stem}_{class_name}_{idx}.jpg"
            crop_path = class_dir / crop_filename
            cv2.imwrite(str(crop_path), crop)

            total_crops += 1
            summary.append((image_path.name, class_name, float(confidence)))
            print(f"  Saved {class_name} (confidence {float(confidence):.2f}) -> {crop_path}")

    print("\n" + "=" * 60)
    print(f"Done. {total_crops} crops saved across {len(image_paths)} photo(s).")
    print(f"Crops:     {crops_dir}")
    print(f"Annotated: {annotated_dir}")
    print("=" * 60)

    if summary:
        low_conf = [s for s in summary if s[2] < 0.5]
        if low_conf:
            print(f"\n{len(low_conf)} detection(s) had confidence below 0.5 -- worth a closer look:")
            for image_name, class_name, confidence in low_conf:
                print(f"  {image_name}: {class_name} ({confidence:.2f})")

    print(
        "\nNext step: open the 'crops' folder and look at each field image. "
        "Ask yourself -- can I read this text myself? If yes for most cards, "
        "you're ready to move on to the GPT-4o OCR step. If a lot of crops "
        "are blurry, cut off, or wrong, that's worth fixing before adding GPT-4o."
    )


if __name__ == "__main__":
    main()
