"""
[C] upload_to_roboflow.py

Uploads a folder of card photos to your Roboflow project via their API,
so you don't have to drag-and-drop 60+ files through the browser one at
a time. Works for either dataset folder (Panini or One Piece) -- just
point --images-dir at whichever one has new photos.

This ONLY uploads the raw images. Annotating them (drawing the 5 boxes:
year, set_name, set_logo, card_number, player_name) still happens in the
Roboflow web UI -- but since you already have a trained model, turn on
"Model-Assisted Labeling" there so it pre-labels the boxes for you and
you just review/correct instead of drawing from scratch.

## One-time setup

Install the roboflow package (not yet in requirements.txt, this is a new
addition just for this script):

    pip install roboflow

Your ROBOFLOW_API_KEY env var (already set from Part 1 of the OCR
pipeline) is reused here -- no new key needed.

## Before running

Open your Roboflow project in the browser and confirm the workspace and
project IDs match the defaults below (Settings page shows both). The
workspace ID is already confirmed from your model ID
(bradys-workspace-wqkgm), but the project slug is a guess based on that
same string -- verify it before your first real upload.

## Running it

Upload every image in the Panini folder that hasn't been uploaded yet:

    python "[C] upload_to_roboflow.py" --images-dir "../Panini"

Upload One Piece photos once you've added more:

    python "[C] upload_to_roboflow.py" --images-dir "../One Piece"

Dry run first (just lists what WOULD be uploaded, uploads nothing):

    python "[C] upload_to_roboflow.py" --images-dir "../Panini" --dry-run
"""

import argparse
import os
import sys

DEFAULT_WORKSPACE = "bradys-workspace-wqkgm"
DEFAULT_PROJECT = "dreamboat-slabs-1-yolov8n-t1"

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}


def find_images(images_dir):
    paths = []
    for name in sorted(os.listdir(images_dir)):
        ext = os.path.splitext(name)[1].lower()
        if ext in IMAGE_EXTENSIONS:
            paths.append(os.path.join(images_dir, name))
    return paths


def main():
    parser = argparse.ArgumentParser(description="Upload card photos to Roboflow")
    parser.add_argument("--images-dir", required=True, help="Folder of images to upload (e.g. ../Panini)")
    parser.add_argument("--workspace", default=DEFAULT_WORKSPACE, help="Roboflow workspace ID")
    parser.add_argument("--project", default=DEFAULT_PROJECT, help="Roboflow project ID")
    parser.add_argument("--dry-run", action="store_true", help="List what would upload without uploading")
    args = parser.parse_args()

    if not os.path.isdir(args.images_dir):
        print(f"Folder not found: {args.images_dir}")
        sys.exit(1)

    images = find_images(args.images_dir)
    if not images:
        print(f"No images found in {args.images_dir}")
        sys.exit(1)

    print(f"Found {len(images)} images in {args.images_dir}")

    if args.dry_run:
        print("\n--dry-run set, not uploading. Would upload:")
        for path in images:
            print(f"  {path}")
        return

    api_key = os.environ.get("ROBOFLOW_API_KEY")
    if not api_key:
        print("No ROBOFLOW_API_KEY found in environment. Set it first (see COMMANDS.md).")
        sys.exit(1)

    try:
        from roboflow import Roboflow
    except ImportError:
        print("roboflow package not installed. Run: pip install roboflow")
        sys.exit(1)

    rf = Roboflow(api_key=api_key)
    project = rf.workspace(args.workspace).project(args.project)

    uploaded, failed = 0, []
    for i, path in enumerate(images, 1):
        name = os.path.basename(path)
        try:
            project.upload(path, batch_name="pre-frontend-punch-list-retrain")
            uploaded += 1
            print(f"[{i}/{len(images)}] uploaded {name}")
        except Exception as e:
            failed.append((name, str(e)))
            print(f"[{i}/{len(images)}] FAILED {name}: {e}")

    print(f"\nDone. {uploaded}/{len(images)} uploaded.")
    if failed:
        print("Failed uploads:")
        for name, err in failed:
            print(f"  {name}: {err}")
        print("\nRe-run the same command -- already-uploaded images will just show as duplicates in Roboflow, safe to retry.")

    print("\nNext step: open your Roboflow project in the browser, turn on")
    print("Model-Assisted Labeling, and review/correct the auto-generated")
    print("boxes on these new images before training.")


if __name__ == "__main__":
    main()
