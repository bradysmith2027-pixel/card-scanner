"""
test_fullcard_scan.py — the detector-free scan path (added 2026-09-15).

WHAT THESE GUARD
    The headline contract is negative and it is the whole reason this mode
    exists: in "fullcard" mode, run_scan MUST NEVER TOUCH THE DETECTOR. If it
    calls _model(), it needs ROBOFLOW_API_KEY, which is not set in Railway —
    so /scan would 502 in production exactly as it does today. A passing scan
    on a laptop with the key set would hide that completely.

    Everything else here protects lessons that were paid for once already:
      - card_type must never be guessed blind (the 2026-07-08
        Panini-reads-as-Topps bug came from defaulting instead of asking)
      - a serial like "9/25" is NOT a card number (found 2026-09-15 — the two
        were colliding in one field and the value was being dropped)
      - the detector path still works, because it was NOT deleted

No network, no OpenAI spend: the client is a fake.
"""

import json
from unittest.mock import patch

import numpy as np
import pytest

from app import scan_service
from app.config import Settings, get_settings
from vision import ocr_card

pytestmark = pytest.mark.unit


# --- fakes -----------------------------------------------------------------
class _Msg:
    def __init__(self, content):
        self.message = type("M", (), {"content": content})()


class _Resp:
    def __init__(self, payload):
        self.choices = [_Msg(json.dumps(payload))]


class FakeOpenAI:
    """Returns queued payloads in order and records every call."""

    def __init__(self, *payloads):
        self._payloads = list(payloads)
        self.calls = []
        completions = type("C", (), {"create": self._create})()
        self.chat = type("Chat", (), {"completions": completions})()

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return _Resp(self._payloads.pop(0))


def _img(h=40, w=30):
    """A real (tiny) BGR array — cv2 is installed, so no need to fake encoding."""
    return np.full((h, w, 3), 128, dtype=np.uint8)


def _ocr_payload(front=None, back=None):
    # Mirrors FIELDS_BY_CARD_TYPE["topps"] — `serial` added by migration 010.
    blank = {"year": None, "set_name": None, "card_number": None,
             "serial": None, "player_name": None}
    return {"front": {**blank, **(front or {})}, "back": {**blank, **(back or {})}}


def _run(payloads, card_type_override="topps", back=b"back", mode="fullcard"):
    """Run run_scan with the vision mode forced and the detector booby-trapped."""
    fake = FakeOpenAI(*payloads)
    settings = Settings()
    settings.scan_vision_mode = mode

    def _boom():
        raise AssertionError(
            "_model() was called in fullcard mode — this needs ROBOFLOW_API_KEY "
            "and would 502 in production."
        )

    with patch.object(scan_service, "_openai", return_value=fake), \
         patch.object(scan_service, "get_settings", return_value=settings), \
         patch.object(scan_service, "_decode", side_effect=lambda b: _img()), \
         patch.object(scan_service, "_model", side_effect=_boom):
        result = scan_service.run_scan(b"front", back, "sports", card_type_override)
    return result, fake


# --- the headline guarantee ------------------------------------------------
def test_fullcard_never_loads_the_detector():
    """If this fails, /scan 502s in production. _model is patched to raise."""
    result, _ = _run([_ocr_payload({"player_name": "LeBron James"})])
    assert result["player_name"] == "LeBron James"
    assert result["vision_mode"] == "fullcard"


def test_fullcard_sends_whole_images_not_crops():
    _, fake = _run([_ocr_payload({"year": "2025"})])
    content = fake.calls[0]["messages"][1]["content"]
    labels = [c["text"] for c in content if c["type"] == "text"]
    images = [c for c in content if c["type"] == "image_url"]
    assert any("FRONT of the card" in t for t in labels)
    assert any("BACK of the card" in t for t in labels)
    # Two whole cards, not five field crops.
    assert len(images) == 2
    # "high" detail is required or small print is unreadable.
    assert all(i["image_url"]["detail"] == "high" for i in images)


def test_front_only_sends_one_image():
    _, fake = _run([_ocr_payload({"player_name": "Sanji"})], back=None)
    content = fake.calls[0]["messages"][1]["content"]
    assert len([c for c in content if c["type"] == "image_url"]) == 1


# --- card_type must never be guessed blind ---------------------------------
def test_unknown_card_type_raises_rather_than_defaulting():
    """The 2026-07-08 bug: defaulting instead of asking mislabels every card."""
    with pytest.raises(scan_service.ScanError):
        _run([{"card_type": None}], card_type_override=None)


def test_card_type_guess_is_used_when_confident():
    result, fake = _run(
        [{"card_type": "panini"}, _ocr_payload({"player_name": "Drake Maye"})],
        card_type_override=None,
    )
    assert result["card_type"] == "panini"
    assert len(fake.calls) == 2  # one guess call, then the OCR call


@pytest.mark.parametrize("bogus", ["Topps", "upper deck", "", "TOPPS "])
def test_card_type_guess_rejects_anything_not_exactly_topps_or_panini(bogus):
    fake = FakeOpenAI({"card_type": bogus})
    assert ocr_card.guess_card_type_from_card(fake, _img()) is None


def test_card_type_guess_handles_missing_image():
    assert ocr_card.guess_card_type_from_card(FakeOpenAI(), None) is None


# --- the serial-vs-card-number trap ---------------------------------------
def test_prompt_tells_the_model_a_serial_is_not_a_card_number():
    """Found 2026-09-15: front read '9/25' (serial), back '127' (card number).

    Both correct, one field — merge_field saw a conflict and dropped the value.
    The prompt has to stop a serial being reported AS the card number.
    """
    prompt = ocr_card.FULLCARD_SYSTEM_PROMPT.lower()
    assert "serial" in prompt
    assert "9/25" in ocr_card.FULLCARD_SYSTEM_PROMPT
    assert "different fields" in prompt and "never be swapped" in prompt


# --- migration 010: the serial now has somewhere to go ---------------------
def test_serial_is_its_own_field_for_sports_cards():
    """Before migration 010 the prompt could TELL a serial from a card number
    but had nowhere to report it, so a correct read was discarded.
    """
    for card_type in ("topps", "panini"):
        fields = ocr_card.FIELDS_BY_CARD_TYPE[card_type]
        assert "serial" in fields
        assert "card_number" in fields  # still distinct, not replaced
        schema = ocr_card.build_schema(card_type)
        for side in ("front", "back"):
            props = schema["schema"]["properties"][side]
            assert "serial" in props["properties"]
            # strict mode: every field must be in `required` or the call errors
            assert "serial" in props["required"]


def test_one_piece_does_not_gain_a_serial_field():
    """OP cards aren't serial-numbered in this sense, the Output Shape spec
    fixes their fields at two, and OP measured 10/10 on 2026-09-15. Adding an
    always-null field to the one card type that reads perfectly is pure risk.
    """
    assert "serial" not in ocr_card.FIELDS_BY_CARD_TYPE["one_piece"]


def test_serial_and_card_number_no_longer_collide():
    """The exact 2026-09-15 failure: front '9/25' (serial), back '127' (card
    number). Both readings correct, one column — merge_field saw a conflict and
    dropped BOTH values. In separate fields each side is the only reading of
    its own field, so nothing conflicts and nothing is lost.
    """
    result, _ = _run([_ocr_payload(
        {"card_number": None, "serial": "9/25"},
        {"card_number": "127", "serial": None},
    )])
    assert result["card_number"] == "127"
    assert result["serial"] == "9/25"
    assert result["needs_review"] == []


def test_unnumbered_card_reports_no_serial():
    """Most cards aren't numbered. Null must stay null — not an empty string,
    which migration 010's CHECK rejects outright."""
    result, _ = _run([_ocr_payload({"serial": None}, {"serial": None})])
    assert result["serial"] is None
    assert "serial" not in result["needs_review"]


def test_conflicting_sides_are_flagged_not_silently_resolved():
    result, _ = _run([_ocr_payload({"card_number": "9/25"}, {"card_number": "127"})])
    assert result["card_number"] is None
    assert "card_number" in result["needs_review"]
    assert result["conflicts"]["card_number"] == {"front": "9/25", "back": "127"}


def test_agreeing_sides_collapse_to_one_value():
    result, _ = _run([_ocr_payload({"player_name": "Ace Bailey"},
                                   {"player_name": "Ace Bailey"})])
    assert result["player_name"] == "Ace Bailey"
    assert result["needs_review"] == []


# --- the detector path still works ----------------------------------------
def test_detector_mode_still_uses_the_detector():
    """YOLO was NOT deleted (2026-08-31 lesson). One env var switches back."""
    fake = FakeOpenAI(_ocr_payload({"player_name": "Cooper Flagg"}))
    settings = Settings()
    settings.scan_vision_mode = "detector"
    called = {}

    class _FakeModel:
        pass

    def _fake_crops(model, image, card_type):
        called["yes"] = True
        return {"player_name": _img()}

    with patch.object(scan_service, "_openai", return_value=fake), \
         patch.object(scan_service, "get_settings", return_value=settings), \
         patch.object(scan_service, "_decode", side_effect=lambda b: _img()), \
         patch.object(scan_service, "_model", return_value=_FakeModel()), \
         patch.object(scan_service, "_crops", side_effect=_fake_crops):
        result = scan_service.run_scan(b"f", b"b", "sports", "topps")

    assert called.get("yes") is True
    assert result["vision_mode"] == "detector"
    assert result["player_name"] == "Cooper Flagg"


# --- config ----------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    (None, "fullcard"),        # default
    ("fullcard", "fullcard"),
    ("detector", "detector"),
    ("DETECTOR", "detector"),  # case-insensitive
    ("nonsense", "fullcard"),  # unknown never silently disables scanning
    ("", "fullcard"),
])
def test_scan_vision_mode_parsing(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("SCAN_VISION_MODE", raising=False)
    else:
        monkeypatch.setenv("SCAN_VISION_MODE", raw)
    get_settings.cache_clear()
    try:
        assert Settings().scan_vision_mode == expected
    finally:
        get_settings.cache_clear()


# --- image handling --------------------------------------------------------
def test_large_photo_is_downscaled_before_upload():
    """Phone photos are huge; the payload has to stay sane."""
    import base64

    import cv2

    encoded = ocr_card.encode_full_image(_img(4000, 3000))
    decoded = cv2.imdecode(
        np.frombuffer(base64.b64decode(encoded), np.uint8), cv2.IMREAD_COLOR
    )
    assert max(decoded.shape[:2]) == ocr_card.FULLCARD_MAX_EDGE_PIXELS


def test_small_photo_is_not_upscaled():
    import base64

    import cv2

    encoded = ocr_card.encode_full_image(_img(40, 30))
    decoded = cv2.imdecode(
        np.frombuffer(base64.b64decode(encoded), np.uint8), cv2.IMREAD_COLOR
    )
    assert decoded.shape[:2] == (40, 30)
