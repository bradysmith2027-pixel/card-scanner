"""
test_merge.py — unit tests for the front/back OCR merge logic in ocr_card.py.

merge_field decides, per field, whether the front and back readings agree,
are compatible (one is a fuller version of the other), or genuinely conflict
(needs manual review). Pure functions — no network, no model. This is the
correctness surface behind "is this the same card read two ways, or a real
disagreement?"

ocr_card lives at the project root (one level above backend/), same as how
scan_service imports it.
"""

import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import ocr_card  # noqa: E402

pytestmark = pytest.mark.unit


# --- merge_field: agreement / compatibility ---------------------------------
def test_exact_match_no_conflict():
    assert ocr_card.merge_field("Prizm", "Prizm") == ("Prizm", None)


def test_case_insensitive_match():
    value, conflict = ocr_card.merge_field("prizm", "PRIZM")
    assert conflict is None and value == "prizm"


def test_subset_keeps_fuller_reading():
    # front read part of the label; back read the whole thing -> not a conflict.
    value, conflict = ocr_card.merge_field("Prizm", "2025 Panini - Prizm Football")
    assert conflict is None
    assert value == "2025 Panini - Prizm Football"


def test_partial_card_number_keeps_complete():
    value, conflict = ocr_card.merge_field("44", "44/99")
    assert conflict is None and value == "44/99"


def test_tie_keeps_front():
    # same words, different order -> tie -> front wins.
    value, conflict = ocr_card.merge_field("Panini Prizm", "Prizm Panini")
    assert conflict is None and value == "Panini Prizm"


# --- merge_field: genuine conflicts -----------------------------------------
def test_different_words_conflict():
    value, conflict = ocr_card.merge_field("Prizm", "Mosaic")
    assert value is None
    assert conflict == {"front": "Prizm", "back": "Mosaic"}


def test_different_numbers_conflict():
    value, conflict = ocr_card.merge_field("44", "45")
    assert value is None
    assert conflict == {"front": "44", "back": "45"}


# --- merge_field: missing sides ---------------------------------------------
def test_only_back_present():
    assert ocr_card.merge_field(None, "Topps") == ("Topps", None)


def test_only_front_present():
    assert ocr_card.merge_field("Topps", None) == ("Topps", None)


def test_both_missing():
    assert ocr_card.merge_field(None, None) == (None, None)


def test_whitespace_only_is_treated_as_missing():
    assert ocr_card.merge_field("   ", "Topps") == ("Topps", None)


# --- normalize ---------------------------------------------------------------
def test_normalize_strips_and_nulls():
    assert ocr_card.normalize("  Prizm  ") == "Prizm"
    assert ocr_card.normalize("   ") is None
    assert ocr_card.normalize(None) is None
