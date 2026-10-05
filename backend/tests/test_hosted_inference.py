"""
test_hosted_inference.py

Tests for running the detector through Roboflow's API.

No network, httpx.post is faked, so these don't cost anything or use credits.
They check the three things most likely to break without anyone noticing:

  1. Confidence gets sent as a percent. The local package uses 0.25 but the API
     wants 0-100. Sending 0.25 would mean 0.25%, so every random box would
     count and the OCR would just look bad instead of erroring.
  2. Boxes come back in the original photo's size. Big photos get shrunk
     before uploading, so the boxes have to be scaled back up.
  3. A 402 (out of credits) turns into a 503 instead of a 502, so the app says
     scanning isn't available instead of "try again".
"""

from unittest.mock import patch

import numpy as np
import pytest

from vision import card_vision

pytestmark = pytest.mark.unit


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"predictions": []}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(
                f"raise_for_status() should not be reached for "
                f"{self.status_code} — it must be handled explicitly."
            )


def _image(width, height):
    return np.zeros((height, width, 3), dtype=np.uint8)


def _prediction(x, y, w, h, name="player_name", conf=0.9):
    return {"x": x, "y": y, "width": w, "height": h, "class": name, "confidence": conf}


def test_confidence_is_sent_as_percent_not_fraction():
    model = card_vision.load_hosted_model("ws/model-1", "key")
    with patch("httpx.post", return_value=_FakeResponse()) as post:
        model.infer(_image(400, 600), confidence=0.25)

    assert post.call_args.kwargs["params"]["confidence"] == 25.0


def test_confidence_already_a_percent_is_not_double_converted():
    model = card_vision.load_hosted_model("ws/model-1", "key")
    with patch("httpx.post", return_value=_FakeResponse()) as post:
        model.infer(_image(400, 600), confidence=40)

    assert post.call_args.kwargs["params"]["confidence"] == 40


def test_small_image_is_not_rescaled():
    payload = {"predictions": [_prediction(100, 200, 50, 20)]}
    model = card_vision.load_hosted_model("ws/model-1", "key")
    with patch("httpx.post", return_value=_FakeResponse(payload=payload)):
        result = model.infer(_image(400, 600), confidence=0.25)

    pred = result["predictions"][0]
    assert (pred["x"], pred["y"], pred["width"], pred["height"]) == (100, 200, 50, 20)


def test_large_image_predictions_are_scaled_back_to_original_pixels():
    # 3200px gets shrunk to 1600px, so the scale is 0.5. A box at x=100 in the
    # shrunk photo is really at x=200.
    payload = {"predictions": [_prediction(100, 150, 40, 30)]}
    model = card_vision.load_hosted_model("ws/model-1", "key")
    with patch("httpx.post", return_value=_FakeResponse(payload=payload)):
        result = model.infer(_image(3200, 2400), confidence=0.25)

    pred = result["predictions"][0]
    assert (pred["x"], pred["y"], pred["width"], pred["height"]) == (200, 300, 80, 60)


def test_scaled_predictions_survive_the_detect_conversion():
    """Goes all the way through detect() to make sure the scaled boxes come out right."""
    payload = {"predictions": [_prediction(100, 150, 40, 30, name="card_number")]}
    model = card_vision.load_hosted_model("ws/model-1", "key")
    with patch("httpx.post", return_value=_FakeResponse(payload=payload)):
        boxes = card_vision.detect(model, _image(3200, 2400), confidence=0.25)

    assert len(boxes) == 1
    box = boxes[0]
    assert box["class_name"] == "card_number"
    # Center (200,300), size 80x60 -> (160,270)-(240,330), plus 6px padding.
    assert (box["x1"], box["y1"]) == (160 - card_vision.PADDING_PIXELS,
                                      270 - card_vision.PADDING_PIXELS)
    assert (box["x2"], box["y2"]) == (240 + card_vision.PADDING_PIXELS,
                                      330 + card_vision.PADDING_PIXELS)


@pytest.mark.parametrize("status", [401, 402, 403])
def test_billing_and_auth_failures_raise_hosted_unavailable(status):
    model = card_vision.load_hosted_model("ws/model-1", "key")
    with patch("httpx.post", return_value=_FakeResponse(status_code=status)):
        with pytest.raises(card_vision.HostedInferenceUnavailable):
            model.infer(_image(400, 600), confidence=0.25)


def test_out_of_credits_surfaces_as_scan_unavailable_not_a_generic_error():
    """A 402 should become ScanUnavailable and then a 503, not a 502."""
    from app.scan_service import ScanUnavailable, _crops

    model = card_vision.load_hosted_model("ws/model-1", "key")
    with patch("httpx.post", return_value=_FakeResponse(status_code=402)):
        with pytest.raises(ScanUnavailable):
            _crops(model, _image(400, 600), "topps")
