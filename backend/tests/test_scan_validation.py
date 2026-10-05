"""
test_scan_validation.py

Tests for the input checks on POST /scan.

run_scan is faked, so no model gets loaded and nothing gets spent on OpenAI.
These check that bad uploads get rejected before anything that costs money
runs, and that errors turn into the right status codes:
  - capture_mode must be sports|tcg           -> 400
  - front must be an image                     -> 415
  - empty file                                 -> 400
  - oversize file                              -> 413
  - a ScanError from the pipeline              -> 422
  - happy path returns the identified fields   -> 200
"""

import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient

from app.main import app
from app.scan_service import ScanError

pytestmark = pytest.mark.unit

client = TestClient(app)

# Fake JPEG bytes. They don't get opened since run_scan is faked.
_IMG = ("front.jpg", b"\xff\xd8\xff\xe0fake-jpeg-bytes", "image/jpeg")


def test_happy_path_returns_result(auth):
    with patch("app.routers.scan.run_scan", return_value={"card_type": "topps", "player": "LeBron James"}):
        resp = client.post("/scan", files={"front": _IMG}, data={"capture_mode": "sports"})
    assert resp.status_code == 200
    assert resp.json()["card_type"] == "topps"


def test_bad_capture_mode_400(auth):
    with patch("app.routers.scan.run_scan", return_value={}):
        resp = client.post("/scan", files={"front": _IMG}, data={"capture_mode": "nonsense"})
    assert resp.status_code == 400


def test_non_image_rejected_415(auth):
    with patch("app.routers.scan.run_scan", return_value={}):
        resp = client.post(
            "/scan",
            files={"front": ("f.txt", b"hello", "text/plain")},
            data={"capture_mode": "sports"},
        )
    assert resp.status_code == 415


def test_empty_file_rejected_400(auth):
    with patch("app.routers.scan.run_scan", return_value={}):
        resp = client.post(
            "/scan",
            files={"front": ("f.jpg", b"", "image/jpeg")},
            data={"capture_mode": "sports"},
        )
    assert resp.status_code == 400


def test_oversize_file_rejected_413(auth, monkeypatch):
    monkeypatch.setattr("app.routers.scan.MAX_UPLOAD_BYTES", 10)  # lower the limit so the test file can be small
    with patch("app.routers.scan.run_scan", return_value={}):
        resp = client.post(
            "/scan",
            files={"front": ("f.jpg", b"x" * 11, "image/jpeg")},
            data={"capture_mode": "sports"},
        )
    assert resp.status_code == 413


def test_scan_error_maps_to_422(auth):
    with patch("app.routers.scan.run_scan", side_effect=ScanError("no detections")):
        resp = client.post("/scan", files={"front": _IMG}, data={"capture_mode": "sports"})
    assert resp.status_code == 422
