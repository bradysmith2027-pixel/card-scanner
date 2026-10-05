"""
test_merge.py

Tests for how ocr_card.py combines the front and back readings.

For each field, merge_field decides if the front and back match, if one is
just a longer version of the other, or if they actually disagree (and need me
to check). No network or model needed.

ocr_card is in backend/vision/, same as how scan_service imports it.
"""

import pytest

from vision import ocr_card

pytestmark = pytest.mark.unit


# --- merge_field: matching readings ----------------------------------------
def test_exact_match_no_conflict():
    assert ocr_card.merge_field("Prizm", "Prizm") == ("Prizm", None)


def test_case_insensitive_match():
    value, conflict = ocr_card.merge_field("prizm", "PRIZM")
    assert conflict is None and value == "prizm"


def test_subset_keeps_fuller_reading():
    # front read part of it, back read all of it -> not a conflict.
    value, conflict = ocr_card.merge_field("Prizm", "2025 Panini - Prizm Football")
    assert conflict is None
    assert value == "2025 Panini - Prizm Football"


def test_partial_card_number_keeps_complete():
    value, conflict = ocr_card.merge_field("44", "44/99")
    assert conflict is None and value == "44/99"


def test_tie_keeps_front():
    # same words in a different order -> tie -> front wins.
    value, conflict = ocr_card.merge_field("Panini Prizm", "Prizm Panini")
    assert conflict is None and value == "Panini Prizm"


# --- merge_field: real conflicts --------------------------------------------
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
