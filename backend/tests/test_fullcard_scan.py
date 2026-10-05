"""
test_fullcard_scan.py

Tests for full card mode (scanning without the detector).

The most important one: in fullcard mode, run_scan should never touch the
detector. If it calls _model(), it needs ROBOFLOW_API_KEY, which isn't set on
Railway, so /scan would break in production. It would still work on my laptop
since I have the key, so I'd never notice.

The rest are for problems I already ran into once:
  - card_type should never just be guessed (a Panini card got read as Topps
    because it defaulted instead of asking)
  - a serial like "9/25" isn't a card number (they were fighting over the same
    field and the value got thrown out)
  - the detector still works, since I kept it

No network and no OpenAI cost, the client is fake.
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
    """Returns the queued responses in order and keeps track of every call."""

    def __init__(self, *payloads):
        self._payloads = list(payloads)
        self.calls = []
        completions = type("C", (), {"create": self._create})()
        self.chat = type("Chat", (), {"completions": completions})()

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return _Resp(self._payloads.pop(0))


def _img(h=40, w=30):
    """A real (tiny) image array. cv2 is installed so there's no need to fake it."""
    return np.full((h, w, 3), 128, dtype=np.uint8)


def _ocr_payload(front=None, back=None):
    # Same as FIELDS_BY_CARD_TYPE["topps"]. serial came with migration 010.
    blank = {"year": None, "set_name": None, "card_number": None,
             "serial": None, "player_name": None}
    return {"front": {**blank, **(front or {})}, "back": {**blank, **(back or {})}}


def _run(payloads, card_type_override="topps", back=b"back", mode="fullcard"):
    """Run run_scan with the mode set and the detector rigged to blow up if it's used."""
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


# --- the most important test -----------------------------------------------
def test_fullcard_never_loads_the_detector():
    """If this fails, /scan breaks in production. _model is set up to throw an error."""
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
    # Two whole card photos, not five crops.
    assert len(images) == 2
    # Needs "high" detail or small text can't be read.
    assert all(i["image_url"]["detail"] == "high" for i in images)


def test_front_only_sends_one_image():
    _, fake = _run([_ocr_payload({"player_name": "Sanji"})], back=None)
    content = fake.calls[0]["messages"][1]["content"]
    assert len([c for c in content if c["type"] == "image_url"]) == 1


# --- card_type should never just be guessed --------------------------------
def test_unknown_card_type_raises_rather_than_defaulting():
    """If it defaults instead of asking, it labels cards wrong (happened in July)."""
    with pytest.raises(scan_service.ScanError):
        _run([{"card_type": None}], card_type_override=None)


def test_card_type_guess_is_used_when_confident():
    result, fake = _run(
        [{"card_type": "panini"}, _ocr_payload({"player_name": "Drake Maye"})],
        card_type_override=None,
    )
    assert result["card_type"] == "panini"
    assert len(fake.calls) == 2  # one call to guess the brand, then the main call


@pytest.mark.parametrize("bogus", ["Topps", "upper deck", "", "TOPPS "])
def test_card_type_guess_rejects_anything_not_exactly_topps_or_panini(bogus):
    fake = FakeOpenAI({"card_type": bogus})
    assert ocr_card.guess_card_type_from_card(fake, _img()) is None


def test_card_type_guess_handles_missing_image():
    assert ocr_card.guess_card_type_from_card(FakeOpenAI(), None) is None


# --- serial vs card number ------------------------------------------------
def test_prompt_tells_the_model_a_serial_is_not_a_card_number():
    """The front read '9/25' (the serial) and the back read '127' (the card number).

    Both were right but they went in the same field, so merge_field saw a
    conflict and threw it out. The prompt needs to keep the serial out of the
    card number.
    """
    prompt = ocr_card.FULLCARD_SYSTEM_PROMPT.lower()
    assert "serial" in prompt
    assert "9/25" in ocr_card.FULLCARD_SYSTEM_PROMPT
    assert "different fields" in prompt and "never be swapped" in prompt


# --- migration 010: the serial has its own field now ----------------------
def test_serial_is_its_own_field_for_sports_cards():
    """Before migration 010, GPT could tell the serial apart from the card number
    but there was nowhere to put it, so it just got thrown out.
    """
    for card_type in ("topps", "panini"):
        fields = ocr_card.FIELDS_BY_CARD_TYPE[card_type]
        assert "serial" in fields
        assert "card_number" in fields  # still its own field
        schema = ocr_card.build_schema(card_type)
        for side in ("front", "back"):
            props = schema["schema"]["properties"][side]
            assert "serial" in props["properties"]
            # strict mode: every field has to be in required or the call fails
            assert "serial" in props["required"]


def test_one_piece_does_not_gain_a_serial_field():
    """One Piece cards aren't numbered like that, and they already read 10/10 in
    my tests. No reason to add a field that would always be empty.
    """
    assert "serial" not in ocr_card.FIELDS_BY_CARD_TYPE["one_piece"]


def test_serial_and_card_number_no_longer_collide():
    """The exact problem from before: front '9/25' (serial), back '127' (card
    number). Both right, but in one column, so merge_field threw both out. Now
    they're separate fields, so there's nothing to conflict and nothing gets lost.
    """
    result, _ = _run([_ocr_payload(
        {"card_number": None, "serial": "9/25"},
        {"card_number": "127", "serial": None},
    )])
    assert result["card_number"] == "127"
    assert result["serial"] == "9/25"
    # Neither field conflicts anymore, which was the point.
    assert "card_number" not in result["needs_review"]
    assert "serial" not in result["needs_review"]
    # parallel does get flagged, which is right. A numbered card has to be some
    # parallel and this one doesn't name one. See
    # test_numbered_card_with_no_parallel_is_flagged.
    assert result["needs_review"] == ["parallel"]


# --- guessing the parallel ------------------------------------------------
#
# I used to always pick the parallel by hand, so most of these tests are about
# what shouldn't happen. Base vs Silver Prizm can be a 10x price difference, and
# a wrong parallel saved with confidence is worse than a blank because nothing
# will question it later.
def _p(parallel=None, confidence=None, **extra):
    return {**_ocr_payload(**extra), "parallel": parallel,
            "parallel_confidence": confidence}


def test_printed_parallel_is_trusted_and_not_flagged():
    """"high" means the parallel name is printed on the card and was read, not guessed."""
    result, _ = _run([_p("Refractor", "high")])
    assert result["parallel"] == "Refractor"
    assert result["parallel_confidence"] == "high"
    assert "parallel" not in result["needs_review"]


def test_inferred_parallel_is_kept_but_flagged():
    """I want it to guess, but flag it if it isn't sure. A guess from how the card
    looks is still useful, I just need to check it."""
    result, _ = _run([_p("Silver Prizm", "low")])
    assert result["parallel"] == "Silver Prizm"
    assert "parallel" in result["needs_review"]


@pytest.mark.parametrize("confidence", [None, "", "medium", "HIGHISH", 3, "maybe"])
def test_unparseable_confidence_defaults_to_FLAGGED(confidence):
    """If the confidence is missing or weird, flag it. Those are the answers I
    can trust the least, so they definitely shouldn't get through unflagged."""
    result, _ = _run([_p("Gold", confidence)])
    assert result["parallel"] == "Gold"
    assert result["parallel_confidence"] is None
    assert "parallel" in result["needs_review"]


@pytest.mark.parametrize(
    "value", ["Base", "base card", "NONE", "n/a", "unknown", "Regular", "  "]
)
def test_base_card_answers_are_rejected(value):
    """A base card should leave card_type empty. Saving "Base" is basically the
    same mistake as saving the wrong parallel."""
    result, _ = _run([_p(value, "high")])
    assert result["parallel"] is None


def test_card_description_is_rejected_rather_than_stored():
    """The prompt says not to describe the card instead of naming the parallel.
    The length check catches it if it does anyway."""
    result, _ = _run([_p("some kind of shiny silver refractor with a wave pattern "
                         "across the whole front of the card", "low")])
    assert result["parallel"] is None


def test_confidence_is_nulled_when_there_is_no_parallel():
    """No parallel means the confidence doesn't mean anything."""
    result, _ = _run([_p(None, "high")])
    assert result["parallel"] is None
    assert result["parallel_confidence"] is None
    # ...and no serial either, so nothing to flag.
    assert result["needs_review"] == []


def test_numbered_card_with_no_parallel_is_flagged():
    """A numbered card has to be some parallel since base cards aren't numbered.
    So if there's a serial but no parallel, the most important detail is missing
    and it needs to be flagged."""
    result, _ = _run([_p(None, None, front={"serial": "9/25"})])
    assert result["serial"] == "9/25"
    assert result["parallel"] is None
    assert "parallel" in result["needs_review"]


def test_numbered_card_WITH_a_printed_parallel_is_not_flagged():
    """Shouldn't flag anything when the parallel is known."""
    result, _ = _run([_p("Gold", "high", front={"serial": "9/25"})])
    assert "parallel" not in result["needs_review"]


# --- guessing the category ------------------------------------------------
def test_category_is_top_level_not_per_side():
    """The sport is about the whole card, it's not text with a front and back
    reading. If it was per side, the two sides could "disagree" and get flagged
    for nothing."""
    schema = ocr_card.build_schema("topps")["schema"]
    assert "category" in schema["properties"]
    assert "category" in schema["required"]  # strict mode needs it
    for side in ("front", "back"):
        assert "category" not in schema["properties"][side]["properties"]


@pytest.mark.parametrize("value", ["basketball", "football", "one piece", "other"])
def test_recognised_category_is_passed_through(value):
    result, _ = _run([{**_ocr_payload(), "category": value}])
    assert result["category"] == value


@pytest.mark.parametrize("value", ["Basketball", "  FOOTBALL  ", "one piece"])
def test_category_is_normalised_before_matching(value):
    """Caps or extra spaces shouldn't turn a good answer into null."""
    result, _ = _run([{**_ocr_payload(), "category": value}])
    assert result["category"] == value.strip().lower()


@pytest.mark.parametrize(
    "value", ["hoops", "American Football", "", "  ", None, 7, "basketball card"]
)
def test_unrecognised_category_becomes_none(value):
    """The category column has no CHECK constraint, so a bad value would save
    with no error and mess up the category reports. This is the only place it
    gets checked."""
    result, _ = _run([{**_ocr_payload(), "category": value}])
    assert result["category"] is None


def test_missing_category_key_does_not_crash():
    """If category is missing it should just be null, not an error. The rest of
    the scan is still fine."""
    result, _ = _run([_ocr_payload()])
    assert result["category"] is None


def test_unnumbered_card_reports_no_serial():
    """Most cards aren't numbered. Null should stay null, not "", which the
    serial column doesn't allow."""
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


# --- the detector still works ---------------------------------------------
def test_detector_mode_still_uses_the_detector():
    """I kept the YOLO detector. One env var switches back to it."""
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
    ("nonsense", "fullcard"),  # a typo shouldn't turn scanning off
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
    """Phone photos are huge, so they need to get shrunk."""
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
