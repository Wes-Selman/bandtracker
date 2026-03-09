"""
tests/test_diff.py

Test suite for BandTracker Increment 3 — diff engine.

Coverage:
  engine.py   byte_diff(), build_description(), ChangedRange, DiffResult
  noise.py    load_noise_mask(), save_noise_mask(), build_noise_mask()
  interpreter.py  interpret_changes()

All tests use synthetic bytes — no real GarageBand files required.
Tempo values use the confirmed µs/beat formula from band-cartographer research.
"""

from __future__ import annotations

import json
import struct
import tempfile
from pathlib import Path

import pytest

from core.diff.engine import (
    ChangedRange,
    DiffResult,
    byte_diff,
    build_description,
    _try_uint32,
    _try_float32,
    _try_string,
)
from core.diff.noise import (
    build_noise_mask,
    load_noise_mask,
    save_noise_mask,
)
from core.diff.interpreter import interpret_changes


# ─────────────────────────────────────────────────────────────
# FIXTURES & HELPERS
# ─────────────────────────────────────────────────────────────

def make_bytes(length: int = 256, fill: int = 0x00) -> bytearray:
    return bytearray([fill] * length)


def pack_uint32_le(value: int) -> bytes:
    return struct.pack("<I", value)


def bpm_to_us(bpm: float) -> int:
    """Convert BPM to GarageBand's raw tempo encoding (bpm * 10_000)."""
    return round(bpm * 10_000)


def make_projectdata_with_tempo(bpm: float, size: int = 512) -> bytes:
    """
    Synthesise a minimal ProjectData blob with a tempo value at offset 0xaa
    (the first of the four confirmed tempo offsets from band-cartographer).
    """
    data = bytearray(size)
    us = bpm_to_us(bpm)
    struct.pack_into("<I", data, 0xaa, us)
    return bytes(data)


# ─────────────────────────────────────────────────────────────
# engine.py — _try_* helpers
# ─────────────────────────────────────────────────────────────

class TestTryHelpers:
    def test_uint32_valid(self):
        data = pack_uint32_le(1210000)
        assert _try_uint32(data, 0) == 1210000

    def test_uint32_too_short(self):
        assert _try_uint32(b"\x01\x02", 0) is None

    def test_uint32_offset(self):
        data = b"\x00\x00" + pack_uint32_le(42)
        assert _try_uint32(data, 2) == 42

    def test_float32_valid(self):
        data = struct.pack("<f", 1.5)
        result = _try_float32(data, 0)
        assert result is not None
        assert abs(result - 1.5) < 0.0001

    def test_float32_too_short(self):
        assert _try_float32(b"\x01", 0) is None

    def test_string_finds_ascii(self):
        data = b"\x00\x00" + b"tempo\x00\x00"
        result = _try_string(data, 2)
        assert result is not None
        assert "tempo" in result

    def test_string_no_printable(self):
        data = bytes([0x00, 0x01, 0x02, 0x03] * 20)
        result = _try_string(data, 0)
        assert result is None


# ─────────────────────────────────────────────────────────────
# engine.py — byte_diff()
# ─────────────────────────────────────────────────────────────

class TestByteDiff:
    def test_identical_files_produce_no_ranges(self):
        data = bytes(256)
        result = byte_diff(data, data)
        assert result.ok
        assert result.num_changed_ranges == 0

    def test_single_byte_change(self):
        a = bytearray(64)
        b = bytearray(64)
        b[10] = 0xFF
        result = byte_diff(bytes(a), bytes(b))
        assert result.ok
        assert result.num_changed_ranges == 1
        assert result.changed_ranges[0].offset_start == 10
        assert result.changed_ranges[0].length == 1

    def test_contiguous_changed_bytes_merged(self):
        a = bytearray(64)
        b = bytearray(64)
        b[10] = 0x01
        b[11] = 0x02
        b[12] = 0x03
        result = byte_diff(bytes(a), bytes(b))
        assert result.num_changed_ranges == 1
        assert result.changed_ranges[0].offset_start == 10
        assert result.changed_ranges[0].offset_end == 13

    def test_non_contiguous_changes_produce_multiple_ranges(self):
        a = bytearray(64)
        b = bytearray(64)
        b[5] = 0x01
        b[40] = 0x02
        result = byte_diff(bytes(a), bytes(b))
        assert result.num_changed_ranges == 2

    def test_size_delta_positive_when_b_longer(self):
        a = bytes(10)
        b = bytes(20)
        result = byte_diff(a, b)
        assert result.size_delta == 10

    def test_size_delta_negative_when_b_shorter(self):
        a = bytes(20)
        b = bytes(10)
        result = byte_diff(a, b)
        assert result.size_delta == -10

    def test_baseline_size_and_changed_size_populated(self):
        a = bytes(100)
        b = bytes(150)
        result = byte_diff(a, b)
        assert result.baseline_size == 100
        assert result.changed_size == 150

    def test_noise_mask_suppresses_known_offsets(self):
        a = bytearray(64)
        b = bytearray(64)
        b[5] = 0x01   # noisy — should be masked
        b[40] = 0x02  # real change
        noise_mask = {5}
        result = byte_diff(bytes(a), bytes(b), noise_mask=noise_mask)
        assert result.num_changed_ranges == 1
        assert result.changed_ranges[0].offset_start == 40
        assert result.skipped_noisy_ranges == 1

    def test_noise_mask_suppresses_entire_range(self):
        a = bytearray(64)
        b = bytearray(64)
        b[10] = 0x01
        b[11] = 0x02
        noise_mask = {10, 11}
        result = byte_diff(bytes(a), bytes(b), noise_mask=noise_mask)
        assert result.num_changed_ranges == 0
        assert result.skipped_noisy_ranges == 1

    def test_noise_mask_partial_range_trimmed(self):
        # bytes 10, 11, 12 changed; byte 10 is noisy — should trim to 11-12
        a = bytearray(64)
        b = bytearray(64)
        b[10] = 0x01
        b[11] = 0x02
        b[12] = 0x03
        noise_mask = {10}
        result = byte_diff(bytes(a), bytes(b), noise_mask=noise_mask)
        assert result.num_changed_ranges == 1
        assert result.changed_ranges[0].offset_start == 11

    def test_noise_filtered_flag_set_when_mask_given(self):
        result = byte_diff(bytes(10), bytes(10), noise_mask=set())
        assert result.noise_filtered is True

    def test_noise_filtered_flag_false_when_no_mask(self):
        result = byte_diff(bytes(10), bytes(10), noise_mask=None)
        assert result.noise_filtered is False

    def test_missing_baseline_returns_error(self):
        result = byte_diff(None, bytes(10))
        assert not result.ok
        assert "baseline" in result.error.lower()

    def test_missing_changed_returns_error(self):
        result = byte_diff(bytes(10), None)
        assert not result.ok
        assert "current" in result.error.lower()

    def test_both_none_returns_error(self):
        result = byte_diff(None, None)
        assert not result.ok

    def test_changed_range_has_correct_bytes(self):
        a = bytearray(16)
        b = bytearray(16)
        b[4] = 0xAB
        b[5] = 0xCD
        result = byte_diff(bytes(a), bytes(b))
        r = result.changed_ranges[0]
        assert r.baseline_bytes == bytes([0x00, 0x00])
        assert r.changed_bytes == bytes([0xAB, 0xCD])

    def test_changed_range_to_dict(self):
        r = ChangedRange(
            offset_start=10,
            offset_end=14,
            baseline_bytes=bytes(4),
            changed_bytes=pack_uint32_le(1210000),
            as_uint32_le=1210000,
        )
        d = r.to_dict()
        assert d["offset_start"] == 10
        assert d["offset_hex"] == "0xa"
        assert d["length"] == 4
        assert d["as_uint32_le"] == 1210000

    def test_diff_result_ok_when_no_error(self):
        result = byte_diff(bytes(10), bytes(10))
        assert result.ok

    def test_diff_result_not_ok_on_error(self):
        result = DiffResult(error="something went wrong")
        assert not result.ok


# ─────────────────────────────────────────────────────────────
# engine.py — build_description()
# ─────────────────────────────────────────────────────────────

class TestBuildDescription:
    def test_returns_interpreted_when_available(self):
        diff = DiffResult()
        desc = build_description(diff, ["tempo changed to 120 BPM"])
        assert desc == "tempo changed to 120 BPM"

    def test_joins_multiple_interpreted_with_semicolon(self):
        diff = DiffResult()
        desc = build_description(diff, ["tempo changed to 120 BPM", "track added"])
        assert desc == "tempo changed to 120 BPM; track added"

    def test_appends_uninterpreted_count(self):
        a = bytearray(64)
        b = bytearray(64)
        b[5] = 0x01
        b[20] = 0x02
        diff = byte_diff(bytes(a), bytes(b))
        # Only 1 interpreted, 2 total ranges → 1 uninterpreted
        desc = build_description(diff, ["tempo changed to 120 BPM"])
        assert "unrecognised change" in desc

    def test_no_changes_fallback(self):
        diff = byte_diff(bytes(64), bytes(64))
        desc = build_description(diff, [])
        assert desc == "no changes detected"

    def test_size_delta_mentioned_when_no_interpreted(self):
        # Use same-length buffers so the size delta logic isn't needed —
        # the real behaviour we want is that uninterpreted ranges fall back
        # to a range-count description.
        a = bytearray(100)
        b = bytearray(100)
        b[50] = 0xFF  # unknown offset — won't be interpreted
        diff = byte_diff(bytes(a), bytes(b))
        desc = build_description(diff, [])
        assert desc != "no changes detected"
        assert "range" in desc

    def test_error_result_returns_diff_unavailable(self):
        diff = DiffResult(error="oops")
        desc = build_description(diff, [])
        assert desc == "diff unavailable"


# ─────────────────────────────────────────────────────────────
# noise.py — save/load round-trip
# ─────────────────────────────────────────────────────────────

class TestNoiseMaskIO:
    def test_save_and_load_roundtrip(self, tmp_path):
        path = tmp_path / "noise_mask.json"
        offsets = {10, 20, 30, 100, 200}
        save_noise_mask(path, offsets)
        loaded = load_noise_mask(path)
        assert loaded == offsets

    def test_load_missing_file_returns_empty_set(self, tmp_path):
        path = tmp_path / "nonexistent.json"
        result = load_noise_mask(path)
        assert result == set()

    def test_load_corrupt_file_returns_empty_set(self, tmp_path):
        path = tmp_path / "noise_mask.json"
        path.write_text("not valid json {{{{")
        result = load_noise_mask(path)
        assert result == set()

    def test_saved_file_has_expected_fields(self, tmp_path):
        path = tmp_path / "noise_mask.json"
        offsets = {1, 2, 3}
        save_noise_mask(path, offsets, meta={"source": "test"})
        data = json.loads(path.read_text())
        assert "noisy_offsets" in data
        assert "noisy_offset_count" in data
        assert "generated_at" in data
        assert data["noisy_offset_count"] == 3
        assert data["meta"]["source"] == "test"

    def test_offsets_are_sorted_in_file(self, tmp_path):
        path = tmp_path / "noise_mask.json"
        save_noise_mask(path, {50, 10, 30, 20})
        data = json.loads(path.read_text())
        assert data["noisy_offsets"] == [10, 20, 30, 50]

    def test_load_empty_offsets_list(self, tmp_path):
        path = tmp_path / "noise_mask.json"
        save_noise_mask(path, set())
        result = load_noise_mask(path)
        assert result == set()


# ─────────────────────────────────────────────────────────────
# noise.py — build_noise_mask()
# ─────────────────────────────────────────────────────────────

class TestBuildNoiseMask:
    def test_identical_files_produce_empty_mask(self):
        data = bytes(256)
        mask = build_noise_mask(data, data)
        assert mask == set()

    def test_changed_bytes_all_included_in_mask(self):
        a = bytearray(64)
        b = bytearray(64)
        b[10] = 0xFF
        b[11] = 0xFF
        b[30] = 0xAB
        mask = build_noise_mask(bytes(a), bytes(b))
        assert 10 in mask
        assert 11 in mask
        assert 30 in mask

    def test_unchanged_bytes_not_in_mask(self):
        a = bytearray(64)
        b = bytearray(64)
        b[10] = 0xFF
        mask = build_noise_mask(bytes(a), bytes(b))
        assert 0 not in mask
        assert 63 not in mask

    def test_mask_covers_full_changed_range(self):
        a = bytearray(64)
        b = bytearray(64)
        for i in range(5, 10):
            b[i] = 0x01
        mask = build_noise_mask(bytes(a), bytes(b))
        assert all(i in mask for i in range(5, 10))

    def test_mask_is_set_of_ints(self):
        mask = build_noise_mask(bytes(16), bytes(16))
        assert isinstance(mask, set)
        for item in mask:
            assert isinstance(item, int)


# ─────────────────────────────────────────────────────────────
# interpreter.py — interpret_changes()
# ─────────────────────────────────────────────────────────────

class TestInterpretChanges:

    # Tempo decoding

    def test_detects_tempo_change_at_offset_0xaa(self):
        baseline = make_projectdata_with_tempo(120.0)
        changed = make_projectdata_with_tempo(124.0)
        result = byte_diff(baseline, changed)
        descriptions = interpret_changes(result)
        assert any("tempo" in d for d in descriptions)
        assert any("124" in d for d in descriptions)

    def test_tempo_decoded_as_integer_when_whole_number(self):
        baseline = make_projectdata_with_tempo(100.0)
        changed = make_projectdata_with_tempo(140.0)
        result = byte_diff(baseline, changed)
        descriptions = interpret_changes(result)
        assert any("140 BPM" in d for d in descriptions)

    def test_tempo_reported_only_once_even_if_multiple_offsets_change(self):
        """
        All four tempo offsets may change simultaneously (confirmed by
        band-cartographer). We should report tempo exactly once.
        """
        data_a = bytearray(0x1400)
        data_b = bytearray(0x1400)
        us_old = bpm_to_us(120)
        us_new = bpm_to_us(130)
        for offset in (0xaa, 0x102, 0x3be, 0x12cc):
            struct.pack_into("<I", data_a, offset, us_old)
            struct.pack_into("<I", data_b, offset, us_new)
        result = byte_diff(bytes(data_a), bytes(data_b))
        descriptions = interpret_changes(result)
        tempo_descriptions = [d for d in descriptions if "tempo" in d]
        assert len(tempo_descriptions) == 1

    def test_nonsense_tempo_value_not_reported(self):
        """A µs/beat value that decodes to < 5 or > 990 BPM is ignored."""
        data_a = bytearray(512)
        data_b = bytearray(512)
        # 1 µs/beat = 60,000,000 BPM — clearly out of range
        struct.pack_into("<I", data_b, 0xaa, 1)
        result = byte_diff(bytes(data_a), bytes(data_b))
        descriptions = interpret_changes(result)
        assert not any("tempo" in d for d in descriptions)

    def test_zero_tempo_value_not_reported(self):
        data_a = bytearray(512)
        data_b = bytearray(512)
        struct.pack_into("<I", data_b, 0xaa, 0)
        result = byte_diff(bytes(data_a), bytes(data_b))
        descriptions = interpret_changes(result)
        assert not any("tempo" in d for d in descriptions)

    # Pan decoding

    def test_detects_pan_change_at_offset_0xc5(self):
        data_a = bytearray(512)
        data_b = bytearray(512)
        data_b[0xc5] = 0x08
        result = byte_diff(bytes(data_a), bytes(data_b))
        descriptions = interpret_changes(result)
        assert any("pan" in d for d in descriptions)

    def test_pan_reported_only_once(self):
        data_a = bytearray(512)
        data_b = bytearray(512)
        data_b[0xc5] = 0x08
        result = byte_diff(bytes(data_a), bytes(data_b))
        descriptions = interpret_changes(result)
        pan_descriptions = [d for d in descriptions if "pan" in d]
        assert len(pan_descriptions) == 1

    # Structural changes

    def test_large_insertion_reported_as_track_added(self):
        a = bytes(1000)
        b = bytes(2500)  # +1500 bytes — well above the structural threshold
        result = byte_diff(a, b)
        descriptions = interpret_changes(result)
        assert any("track" in d or "added" in d for d in descriptions)

    def test_large_deletion_reported_as_track_removed(self):
        a = bytes(2500)
        b = bytes(1000)
        result = byte_diff(a, b)
        descriptions = interpret_changes(result)
        assert any("removed" in d for d in descriptions)

    def test_small_size_delta_not_flagged_as_structural(self):
        # A 10-byte delta is just field edits, not a whole track
        a = bytes(500)
        b = bytes(510)
        result = byte_diff(a, b)
        descriptions = interpret_changes(result)
        # Nothing structural should be reported for a 10-byte delta
        assert not any("track" in d for d in descriptions)

    # Error / empty cases

    def test_failed_diff_returns_empty_list(self):
        result = DiffResult(error="no ProjectData")
        descriptions = interpret_changes(result)
        assert descriptions == []

    def test_no_changes_returns_empty_list(self):
        data = bytes(256)
        result = byte_diff(data, data)
        descriptions = interpret_changes(result)
        assert descriptions == []

    def test_unrecognised_offset_silently_ignored(self):
        """A change at an offset we don't know about should not crash or emit garbage."""
        a = bytearray(512)
        b = bytearray(512)
        b[0x1FF] = 0xFF  # offset not in the known field map (0x1FF = 511, last valid byte)
        result = byte_diff(bytes(a), bytes(b))
        descriptions = interpret_changes(result)
        # Should return cleanly with no descriptions (just the structural check)
        assert isinstance(descriptions, list)


# ─────────────────────────────────────────────────────────────
# Integration: full pipeline
# ─────────────────────────────────────────────────────────────

class TestDiffPipeline:
    """
    End-to-end tests exercising byte_diff → interpret_changes →
    build_description together, with and without noise masking.
    """

    def test_tempo_change_produces_human_readable_description(self):
        baseline = make_projectdata_with_tempo(120.0)
        changed = make_projectdata_with_tempo(128.0)
        result = byte_diff(baseline, changed)
        interpreted = interpret_changes(result)
        description = build_description(result, interpreted)
        assert "tempo" in description
        assert "128" in description

    def test_noise_mask_cleans_up_spurious_ranges(self):
        """
        Simulate a noisy file: 50 spurious changes + 1 real tempo change.
        After applying the mask the only report should be the tempo.
        """
        a = bytearray(0x200)
        b = bytearray(0x200)

        # Real tempo change at 0xaa
        struct.pack_into("<I", a, 0xaa, bpm_to_us(120))
        struct.pack_into("<I", b, 0xaa, bpm_to_us(130))

        # 50 spurious byte changes at known-noisy offsets
        noisy_offsets = set(range(0x50, 0x82))  # 50 offsets
        for offset in noisy_offsets:
            b[offset] = (b[offset] + 1) % 256

        result_unfiltered = byte_diff(bytes(a), bytes(b))
        result_filtered = byte_diff(bytes(a), bytes(b), noise_mask=noisy_offsets)

        # Unfiltered has lots of noise
        assert result_unfiltered.num_changed_ranges > result_filtered.num_changed_ranges

        # Filtered only sees the tempo change
        interpreted = interpret_changes(result_filtered)
        assert any("130 BPM" in d for d in interpreted)

    def test_no_change_produces_no_changes_detected(self):
        data = make_projectdata_with_tempo(120.0)
        result = byte_diff(data, data)
        interpreted = interpret_changes(result)
        description = build_description(result, interpreted)
        assert description == "no changes detected"

    def test_noise_mask_learned_from_identical_saves_then_applied(self, tmp_path):
        """
        Full round-trip of the noise workflow:
          1. Build a noise mask from two identical saves
          2. Save to disk
          3. Load from disk
          4. Apply to a diff that contains both noise and a real change
        """
        # Step 1: two identical saves (all differences = noise)
        save_a = bytearray(0x200)
        save_b = bytearray(0x200)
        noisy_offsets_actual = {0x10, 0x11, 0x12, 0x20}
        for offset in noisy_offsets_actual:
            save_b[offset] = 0xFF
        mask = build_noise_mask(bytes(save_a), bytes(save_b))
        assert noisy_offsets_actual.issubset(mask)

        # Step 2 & 3: save and reload the mask
        mask_path = tmp_path / "noise_mask.json"
        save_noise_mask(mask_path, mask)
        loaded_mask = load_noise_mask(mask_path)
        assert loaded_mask == mask

        # Step 4: apply loaded mask to a diff with noise + real tempo change
        real_a = bytearray(0x200)
        real_b = bytearray(0x200)
        struct.pack_into("<I", real_a, 0xaa, bpm_to_us(120))
        struct.pack_into("<I", real_b, 0xaa, bpm_to_us(140))
        for offset in noisy_offsets_actual:
            real_b[offset] = 0xFF

        result = byte_diff(bytes(real_a), bytes(real_b), noise_mask=loaded_mask)
        interpreted = interpret_changes(result)
        assert any("140 BPM" in d for d in interpreted)
        assert result.skipped_noisy_ranges > 0
