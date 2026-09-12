"""An OCR offset is seconds, never hours (2026-09-12)."""
import math

from logfather.core.time_alignment import MAX_PLAUSIBLE_OCR_OFFSET_S, plausible_ocr_offset


def test_small_offsets_pass_and_hours_fail():
    assert plausible_ocr_offset(-7.9)
    assert plausible_ocr_offset(0)
    assert plausible_ocr_offset(MAX_PLAUSIBLE_OCR_OFFSET_S)
    assert not plausible_ocr_offset(-24774.2)   # the cached value that hid the playhead
    assert not plausible_ocr_offset(MAX_PLAUSIBLE_OCR_OFFSET_S + 1)


def test_garbage_fails():
    assert not plausible_ocr_offset(None)
    assert not plausible_ocr_offset("abc")
    assert not plausible_ocr_offset(math.nan)
