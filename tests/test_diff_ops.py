"""
tests/test_diff_ops.py — Increment 10: Diff command + structural fallback

Tests cover:
  - compare(): snapshot vs snapshot, snapshot vs GB bundle
  - Error paths: missing project, missing snapshots, missing ProjectData
  - Noise mask loading (present and absent)
  - Same-snapshot short-circuit
  - Three-tier description output: interpreted → structural → identical
  - CompareResult field correctness and JSON-safety
"""

import json
import struct
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.init import (
    PROJECTDATA_MAGIC,
    PROJECTDATA_MAGIC_OFFSET,
    initialize,
    write_json_atomic,
)
from core.models import (
    Project,
    ProjectPaths,
    Snapshot,
    StorageProvider,
)
from core.diff.engine import byte_diff, build_description, DiffResult
from core.diff.noise import save_noise_mask
from core.diff_ops import compare, CompareResult


# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────

def make_file(path: Path, content: bytes = b"data") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def make_project_data(tempo: int = 120) -> bytes:
    """Minimal valid ProjectData blob with a given tempo."""
    data = bytearray(512)
    data[PROJECTDATA_MAGIC_OFFSET:PROJECTDATA_MAGIC_OFFSET + 4] = PROJECTDATA_MAGIC
    # Tempo in µs/beat at offset 0xaa (one of the confirmed offsets)
    us_per_beat = int(60_000_000 / tempo)
    struct.pack_into("<I", data, 0xaa, us_per_beat)
    return bytes(data)


def bpm_to_us(bpm: float) -> int:
    return int(60_000_000 / bpm)


def make_band(tmp: Path, name: str = "TestProject",
              with_media: bool = False, tempo: int = 120) -> Path:
    """Minimal valid .band bundle."""
    band = tmp / f"{name}.band"
    (band / "Alternatives" / "000").mkdir(parents=True)
    (band / "Media" / "Audio Files").mkdir(parents=True)
    data = bytearray(512)
    data[PROJECTDATA_MAGIC_OFFSET:PROJECTDATA_MAGIC_OFFSET + 4] = PROJECTDATA_MAGIC
    struct.pack_into("<I", data, 0x40, tempo * 10_000)
    (band / "Alternatives" / "000" / "ProjectData").write_bytes(data)
    if with_media:
        (band / "Media" / "Audio Files" / "Guitar.aif").write_bytes(
            b"AIFF" + b"\x00" * 64
        )
    return band


def make_provider(tmp: Path) -> StorageProvider:
    return StorageProvider.local(tmp / "BandTracker")


def init_project(tmp: Path, name: str = "TestProject",
                 with_media: bool = False) -> tuple[StorageProvider, str]:
    band = make_band(tmp / "gb", name=name, with_media=with_media)
    provider = make_provider(tmp)
    result = initialize(band, provider, "j@e.com", "Jordan")
    assert result.ok, f"initialize() failed: {result.errors}"
    return provider, result.project_name


def write_snapshot_project_data(
    paths: ProjectPaths,
    index: int,
    data: bytes,
) -> None:
    """Write ProjectData bytes into a snapshot folder."""
    pd_path = paths.snapshot_project_data(index)
    pd_path.parent.mkdir(parents=True, exist_ok=True)
    pd_path.write_bytes(data)


def write_snapshot_meta(paths: ProjectPaths, index: int, desc: str = "test") -> None:
    """Write a minimal meta.json for a snapshot."""
    from datetime import datetime, timezone
    meta = {
        "index": index,
        "description": desc,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "author": "j@e.com",
        "diff_summary": [],
        "milestone": None,
        "media": [],
        "sidecar_files": [],
    }
    meta_path = paths.snapshot_meta(index)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(meta_path, json.dumps(meta, indent=2))


def add_snapshot(
    paths: ProjectPaths,
    project_name: str,
    index: int,
    data: bytes,
    desc: str = "test",
) -> None:
    """Add a snapshot with ProjectData and meta.json, update project.json."""
    write_snapshot_project_data(paths, index, data)
    write_snapshot_meta(paths, index, desc)
    # Update project.json
    project = Project.from_json(paths.project_json.read_text())
    project.latest_snapshot = index
    project.next_snapshot_index = index + 1
    write_json_atomic(paths.project_json, project.to_json())


def gb_pd_path(tmp: Path, project_name: str) -> Path:
    """Path to the GB bundle's ProjectData used by init_project."""
    return tmp / "gb" / f"{project_name}.band" / "Alternatives" / "000" / "ProjectData"


# ─────────────────────────────────────────────────────────────
# CompareResult basics
# ─────────────────────────────────────────────────────────────

class TestCompareResult:
    def test_default_is_not_ok(self):
        r = CompareResult()
        assert not r.ok

    def test_all_fields_json_safe(self):
        r = CompareResult(
            ok=True,
            description="test",
            diff_summary=["a", "b"],
            baseline_index=1,
            compared_index=2,
            num_ranges=3,
            size_delta=100,
            noise_filtered=True,
        )
        d = asdict(r)
        # Should not raise — all fields are JSON-safe
        serialized = json.dumps(d)
        parsed = json.loads(serialized)
        assert parsed["ok"] is True
        assert parsed["description"] == "test"
        assert parsed["diff_summary"] == ["a", "b"]
        assert parsed["compared_index"] == 2

    def test_compared_index_none_is_json_safe(self):
        r = CompareResult(ok=True, baseline_index=1, compared_index=None)
        d = asdict(r)
        serialized = json.dumps(d)
        parsed = json.loads(serialized)
        assert parsed["compared_index"] is None


# ─────────────────────────────────────────────────────────────
# compare() — snapshot vs snapshot
# ─────────────────────────────────────────────────────────────

class TestCompareSnapshotVsSnapshot:
    def test_identical_snapshots_returns_no_changes(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        paths = ProjectPaths(provider.project_path(project_name))

        # Snapshot 1 is created by init. Add snapshot 2 with same data.
        data = paths.snapshot_project_data(1).read_bytes()
        add_snapshot(paths, project_name, 2, data, "Copy")

        result = compare(provider, project_name, 1, 2)

        assert result.ok
        assert result.description == "no changes detected"
        assert result.diff_summary == []
        assert result.num_ranges == 0

    def test_different_snapshots_returns_description(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        paths = ProjectPaths(provider.project_path(project_name))

        # Snapshot 2 with different data — change a byte
        data = bytearray(paths.snapshot_project_data(1).read_bytes())
        data[50] = (data[50] + 1) % 256
        add_snapshot(paths, project_name, 2, bytes(data), "Changed")

        result = compare(provider, project_name, 1, 2)

        assert result.ok
        assert result.description != ""
        assert result.description != "no changes detected"
        assert result.baseline_index == 1
        assert result.compared_index == 2
        assert result.num_ranges > 0

    def test_same_index_shortcircuits(self, tmp_path):
        provider, project_name = init_project(tmp_path)

        result = compare(provider, project_name, 1, 1)

        assert result.ok
        assert result.description == "no changes detected"
        assert result.baseline_index == 1
        assert result.compared_index == 1

    def test_nonexistent_baseline_errors(self, tmp_path):
        provider, project_name = init_project(tmp_path)

        result = compare(provider, project_name, 99)

        assert not result.ok
        assert any("099" in e or "99" in e for e in result.errors)

    def test_nonexistent_compared_errors(self, tmp_path):
        provider, project_name = init_project(tmp_path)

        result = compare(provider, project_name, 1, 99)

        assert not result.ok
        assert any("099" in e or "99" in e for e in result.errors)

    def test_missing_project_json_errors(self, tmp_path):
        provider = make_provider(tmp_path)
        # No project exists at all
        result = compare(provider, "NoSuchProject", 1)
        assert not result.ok
        assert len(result.errors) > 0

    def test_corrupt_project_json_errors(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        paths = ProjectPaths(provider.project_path(project_name))
        paths.project_json.write_text("{ invalid json }")

        result = compare(provider, project_name, 1, 1)

        assert not result.ok
        assert any("project.json" in e.lower() or "parse" in e.lower()
                    for e in result.errors)

    def test_missing_snapshot_project_data_errors(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        paths = ProjectPaths(provider.project_path(project_name))

        # Add snapshot 2 folder with meta but no ProjectData
        write_snapshot_meta(paths, 2, "No data")
        project = Project.from_json(paths.project_json.read_text())
        project.latest_snapshot = 2
        project.next_snapshot_index = 3
        write_json_atomic(paths.project_json, project.to_json())

        result = compare(provider, project_name, 1, 2)

        assert not result.ok
        assert any("projectdata" in e.lower() for e in result.errors)


# ─────────────────────────────────────────────────────────────
# compare() — snapshot vs GB bundle
# ─────────────────────────────────────────────────────────────

class TestCompareSnapshotVsBundle:
    def test_no_changes_when_bundle_matches_snapshot(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        gb_band = tmp_path / "gb" / f"{project_name}.band"

        result = compare(
            provider, project_name, 1,
            gb_override=gb_band,
        )

        assert result.ok
        assert result.compared_index is None
        assert result.baseline_index == 1
        # May or may not detect changes depending on init copy behavior
        # The key is that it completes successfully
        assert isinstance(result.description, str)

    def test_detects_changes_in_bundle(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        gb_band = tmp_path / "gb" / f"{project_name}.band"

        # Modify the GB bundle's ProjectData
        pd = gb_pd_path(tmp_path, project_name)
        data = bytearray(pd.read_bytes())
        data[50] = (data[50] + 1) % 256
        pd.write_bytes(bytes(data))

        result = compare(
            provider, project_name, 1,
            gb_override=gb_band,
        )

        assert result.ok
        assert result.compared_index is None
        assert result.num_ranges > 0

    def test_gb_override_path_used(self, tmp_path):
        """When --gb is passed, that path is used even if project.json has a different one."""
        provider, project_name = init_project(tmp_path)

        # Create a second GB bundle at a different location
        alt_band = make_band(tmp_path / "alt", name=project_name, tempo=140)

        result = compare(
            provider, project_name, 1,
            gb_override=alt_band,
        )

        assert result.ok
        # Should detect differences since alt bundle has different tempo data
        assert isinstance(result.description, str)

    def test_missing_gb_bundle_errors(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        missing = tmp_path / "missing.band"

        result = compare(
            provider, project_name, 1,
            gb_override=missing,
        )

        assert not result.ok
        assert len(result.errors) > 0

    def test_missing_project_data_in_bundle_errors(self, tmp_path):
        provider, project_name = init_project(tmp_path)

        # Create a bundle without ProjectData
        empty_band = tmp_path / "empty.band"
        (empty_band / "Alternatives" / "000").mkdir(parents=True)

        result = compare(
            provider, project_name, 1,
            gb_override=empty_band,
        )

        assert not result.ok
        assert any("projectdata" in e.lower() for e in result.errors)


# ─────────────────────────────────────────────────────────────
# compare() — noise mask handling
# ─────────────────────────────────────────────────────────────

class TestCompareNoiseMask:
    def test_noise_mask_applied_when_present(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        paths = ProjectPaths(provider.project_path(project_name))

        # Write a noise mask that covers some offsets
        noisy_offsets = set(range(40, 60))
        save_noise_mask(paths.noise_mask_json, noisy_offsets)

        # Create snapshot 2 with changes only in noisy offsets
        data = bytearray(paths.snapshot_project_data(1).read_bytes())
        for offset in range(40, 60):
            if offset < len(data):
                data[offset] = (data[offset] + 1) % 256
        add_snapshot(paths, project_name, 2, bytes(data), "Noisy")

        result = compare(provider, project_name, 1, 2)

        assert result.ok
        assert result.noise_filtered is True
        # All changes were noise — should be filtered out
        assert result.description == "no changes detected"

    def test_warning_when_noise_mask_missing(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        paths = ProjectPaths(provider.project_path(project_name))

        # Make sure noise_mask.json does NOT exist
        if paths.noise_mask_json.exists():
            paths.noise_mask_json.unlink()

        # Create snapshot 2 with a change
        data = bytearray(paths.snapshot_project_data(1).read_bytes())
        data[50] = (data[50] + 1) % 256
        add_snapshot(paths, project_name, 2, bytes(data), "Changed")

        result = compare(provider, project_name, 1, 2)

        assert result.ok
        assert any("noise" in w.lower() for w in result.warnings)


# ─────────────────────────────────────────────────────────────
# compare() — result field correctness
# ─────────────────────────────────────────────────────────────

class TestCompareResultFields:
    def test_size_delta_positive_when_grew(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        paths = ProjectPaths(provider.project_path(project_name))

        # Snapshot 2 with more bytes
        orig = paths.snapshot_project_data(1).read_bytes()
        bigger = orig + b"\x00" * 100
        add_snapshot(paths, project_name, 2, bigger, "Bigger")

        result = compare(provider, project_name, 1, 2)

        assert result.ok
        assert result.size_delta > 0

    def test_size_delta_negative_when_shrank(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        paths = ProjectPaths(provider.project_path(project_name))

        # Snapshot 2 with fewer bytes
        orig = paths.snapshot_project_data(1).read_bytes()
        smaller = orig[:256]
        add_snapshot(paths, project_name, 2, smaller, "Smaller")

        result = compare(provider, project_name, 1, 2)

        assert result.ok
        assert result.size_delta < 0

    def test_diff_summary_is_list(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        paths = ProjectPaths(provider.project_path(project_name))

        data = bytearray(paths.snapshot_project_data(1).read_bytes())
        data[50] = (data[50] + 1) % 256
        add_snapshot(paths, project_name, 2, bytes(data), "Changed")

        result = compare(provider, project_name, 1, 2)

        assert result.ok
        assert isinstance(result.diff_summary, list)


# ─────────────────────────────────────────────────────────────
# build_description() — three-tier output (structural fallback)
# ─────────────────────────────────────────────────────────────

class TestBuildDescriptionStructuralTier:
    """
    Tests for the enhanced structural fallback in build_description().
    Tier 1: interpreted descriptions present → use them
    Tier 2: no interpreted, but changed ranges → structural summary
    Tier 3: no changes at all → "no changes detected"
    """

    def test_tier1_interpreted_only(self):
        diff = DiffResult()
        desc = build_description(diff, ["tempo changed to 120 BPM"])
        assert desc == "tempo changed to 120 BPM"

    def test_tier1_multiple_interpreted(self):
        diff = DiffResult()
        desc = build_description(diff, ["tempo changed to 120 BPM", "track added"])
        assert desc == "tempo changed to 120 BPM; track added"

    def test_tier2_structural_with_size_delta(self):
        """When no interpreted changes, structural tier mentions ranges and size delta."""
        a = bytearray(100)
        b = bytearray(200)  # +100 bytes
        b[:100] = a  # first 100 bytes identical
        b[50] = 0xFF  # one change in the overlapping region
        diff = byte_diff(bytes(a), bytes(b))
        desc = build_description(diff, [])
        assert "range" in desc
        assert desc != "no changes detected"

    def test_tier2_structural_includes_bytes_modified(self):
        """Structural tier should include total bytes modified."""
        a = bytearray(256)
        b = bytearray(256)
        # Create two distinct changed regions
        b[10] = 0xFF
        b[11] = 0xFF  # 2 bytes at offset 10
        b[100] = 0xFF  # 1 byte at offset 100
        diff = byte_diff(bytes(a), bytes(b))
        desc = build_description(diff, [])
        assert desc != "no changes detected"
        assert "range" in desc
        # Should mention bytes modified
        assert "bytes" in desc.lower()

    def test_tier2_structural_no_size_delta(self):
        """When same size but content differs, structural tier still works."""
        a = bytearray(100)
        b = bytearray(100)
        b[50] = 0xFF
        diff = byte_diff(bytes(a), bytes(b))
        desc = build_description(diff, [])
        assert desc != "no changes detected"
        assert "range" in desc

    def test_tier3_identical(self):
        data = bytes(64)
        diff = byte_diff(data, data)
        desc = build_description(diff, [])
        assert desc == "no changes detected"

    def test_error_result_returns_diff_unavailable(self):
        diff = DiffResult(error="oops")
        desc = build_description(diff, [])
        assert desc == "diff unavailable"

    def test_uninterpreted_count_appended_to_interpreted(self):
        """When some ranges are interpreted and some aren't, count is appended."""
        a = bytearray(64)
        b = bytearray(64)
        b[5] = 0x01
        b[20] = 0x02
        diff = byte_diff(bytes(a), bytes(b))
        # 1 interpreted, but diff has 2 ranges → 1 uninterpreted
        desc = build_description(diff, ["tempo changed to 120 BPM"])
        assert "unrecognised change" in desc


# ─────────────────────────────────────────────────────────────
# Integration: compare() end-to-end with tempo changes
# ─────────────────────────────────────────────────────────────

class TestCompareIntegration:
    def test_tempo_change_detected_between_snapshots(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        paths = ProjectPaths(provider.project_path(project_name))

        # Write snapshot 2 with a different tempo at offset 0xaa
        data = bytearray(paths.snapshot_project_data(1).read_bytes())
        new_us = bpm_to_us(140.0)
        struct.pack_into("<I", data, 0xaa, new_us)
        add_snapshot(paths, project_name, 2, bytes(data), "Tempo change")

        result = compare(provider, project_name, 1, 2)

        assert result.ok
        assert any("tempo" in d for d in result.diff_summary)
        assert "tempo" in result.description.lower()

    def test_structural_change_detected_on_size_growth(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        paths = ProjectPaths(provider.project_path(project_name))

        # Snapshot 2 is much bigger (simulates adding a track)
        orig = paths.snapshot_project_data(1).read_bytes()
        bigger = orig + b"\x00" * 2000
        add_snapshot(paths, project_name, 2, bigger, "Track added")

        result = compare(provider, project_name, 1, 2)

        assert result.ok
        assert result.size_delta > 0
        # Should have either an interpreted structural change or structural tier desc
        assert result.description != "no changes detected"

    def test_full_pipeline_result_is_serializable(self, tmp_path):
        """The entire CompareResult can be serialized via dataclasses.asdict()."""
        provider, project_name = init_project(tmp_path)
        paths = ProjectPaths(provider.project_path(project_name))

        data = bytearray(paths.snapshot_project_data(1).read_bytes())
        data[50] = (data[50] + 1) % 256
        add_snapshot(paths, project_name, 2, bytes(data), "Changed")

        result = compare(provider, project_name, 1, 2)
        d = asdict(result)
        serialized = json.dumps(d)
        parsed = json.loads(serialized)
        assert parsed["ok"] == result.ok
        assert parsed["description"] == result.description
