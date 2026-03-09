"""
tests/test_restore.py — Increment 6

Full test coverage for core/restore.py.

All tests use tmp_path fixtures — no GarageBand required.

Tests construct minimal on-disk project trees directly rather than
calling core/init.py, so the restore tests are independent of
Increment 1's implementation details.

Fixtures use StorageProvider.local(tmp_path) and the real ProjectPaths
API so path construction is identical to production code.

Test taxonomy
─────────────
Precondition failures (nothing written):
  test_missing_project_json
  test_snapshot_index_out_of_range
  test_snapshot_project_data_missing
  test_garageband_lock_present
  test_garageband_process_blocks_without_force
  test_garageband_process_skipped_with_force

Happy path:
  test_restore_replaces_project_data
  test_restore_creates_confirmation_snapshot
  test_restore_dry_run_no_writes
  test_restore_to_earliest_snapshot
  test_restore_to_latest_snapshot_is_valid
  test_restore_leaves_no_temp_files
  test_restore_confirmation_description_contains_target_description

Rollback:
  test_rollback_on_write_failure

Internal helpers:
  test_find_target_description_present
  test_find_target_description_missing_file
  test_garageband_lock_present_true
  test_garageband_lock_absent

RestoreResult.__str__:
  test_result_str_success
  test_result_str_failure
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from core.models import Project, ProjectPaths, Snapshot, StorageProvider
from core.restore import (
    RestoreResult,
    _garageband_lock_present,
    _load_target_snapshot_description,
    restore,
)


# ---------------------------------------------------------------------------
# Fixtures — minimal on-disk project trees
# ---------------------------------------------------------------------------

def _make_provider(tmp_path: Path) -> StorageProvider:
    """StorageProvider rooted at tmp_path/BandTracker — matches production layout."""
    root = tmp_path / "BandTracker"
    root.mkdir(parents=True, exist_ok=True)
    return StorageProvider.local(root)


def _make_project_json(project_root: Path, name: str, latest_snapshot: int) -> None:
    """Write a minimal valid project.json."""
    data = {
        "name": name,
        "uuid": "test-uuid-1234",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "owner": "tester",
        "collaborators": [{"display_name": "Tester", "identifier": "tester"}],
        "garageband_version": None,
        "latest_snapshot": latest_snapshot,
        "next_snapshot_index": latest_snapshot + 1,
        "gb_bundle_path": None,
        "gb_bundle_alias": None,
    }
    (project_root / "project.json").write_text(json.dumps(data))


def _make_snapshot(
    paths: ProjectPaths,
    index: int,
    content: bytes = b"gnoS snapshot data",
    description: str = "Test snapshot",
) -> None:
    """Write a minimal snapshot directory with ProjectData and meta.json."""
    snap_dir = paths.snapshot(index)
    snap_dir.mkdir(parents=True, exist_ok=True)
    paths.snapshot_project_data(index).write_bytes(content)

    meta = {
        "index": index,
        "description": description,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "author": "tester",
        "diff_summary": [],
        "milestone": None,
        "media": [],
        "sidecar_files": [],
    }
    paths.snapshot_meta(index).write_text(json.dumps(meta))
    paths.snapshot_manifest(index).write_text(json.dumps({"entries": []}))


def _make_live_project_data(paths: ProjectPaths, project_name: str, content: bytes) -> Path:
    """Create the live ProjectData file and its parent directories."""
    pd_path = paths.live_project_data(project_name)
    pd_path.parent.mkdir(parents=True, exist_ok=True)
    pd_path.write_bytes(content)
    return pd_path


def _make_full_project(
    tmp_path: Path,
    name: str = "TestProject",
    num_snapshots: int = 2,
) -> tuple[StorageProvider, ProjectPaths]:
    """Build a complete minimal project tree. Returns (provider, paths)."""
    provider = _make_provider(tmp_path)
    project_root = provider.project_path(name)
    project_root.mkdir(parents=True, exist_ok=True)
    paths = ProjectPaths(project_root)

    _make_project_json(project_root, name=name, latest_snapshot=num_snapshots)

    for i in range(1, num_snapshots + 1):
        _make_snapshot(
            paths,
            index=i,
            content=f"gnoS snapshot {i}".encode(),
            description=f"Snapshot {i}",
        )

    _make_live_project_data(paths, name, content=b"gnoS live current")
    paths.media.mkdir(exist_ok=True)
    paths.docs.mkdir(exist_ok=True)

    return provider, paths


# ---------------------------------------------------------------------------
# Patch helper — take_snapshot is called with provider+project_name signature
# ---------------------------------------------------------------------------

class FakeSnapResult:
    def __init__(self, index: int):
        self.ok = True
        self.snapshot_index = index
        self.errors: list[str] = []


def _patch_snapshot(next_index: int = 3):
    return patch(
        "core.restore.take_snapshot",
        return_value=FakeSnapResult(next_index),
    )


# ---------------------------------------------------------------------------
# Precondition failure tests
# ---------------------------------------------------------------------------

class TestPreconditionFailures:

    def test_missing_project_json(self, tmp_path):
        provider = _make_provider(tmp_path)
        provider.projects_path.mkdir(parents=True, exist_ok=True)
        provider.project_path("NoProject").mkdir(parents=True, exist_ok=True)

        result = restore(provider, "NoProject", 1, force=True)

        assert not result.success
        assert any("project.json" in e for e in result.errors)

    def test_snapshot_index_does_not_exist(self, tmp_path):
        provider, _ = _make_full_project(tmp_path, num_snapshots=2)
        with _patch_snapshot():
            result = restore(provider, "TestProject", 99, force=True)

        assert not result.success
        assert any("99" in e or "does not exist" in e.lower() for e in result.errors)

    def test_snapshot_project_data_missing(self, tmp_path):
        """Snapshot dir exists but ProjectData is absent — corrupt snapshot."""
        provider, paths = _make_full_project(tmp_path, num_snapshots=2)
        paths.snapshot_project_data(1).unlink()

        with _patch_snapshot():
            result = restore(provider, "TestProject", 1, force=True)

        assert not result.success
        assert any("missing" in e.lower() or "ProjectData" in e for e in result.errors)

    def test_garageband_lock_present(self, tmp_path):
        provider, paths = _make_full_project(tmp_path)
        # Write the GB lock file inside the live bundle
        lock = paths.live_band("TestProject") / ".lck"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.touch()

        result = restore(provider, "TestProject", 1, force=False)

        assert not result.success
        assert any("lock" in e.lower() or "GarageBand" in e for e in result.errors)

    def test_garageband_process_blocks_without_force(self, tmp_path):
        provider, _ = _make_full_project(tmp_path)
        with patch("core.restore._garageband_process_running", return_value=True):
            result = restore(provider, "TestProject", 1, force=False)

        assert not result.success
        assert any("GarageBand" in e for e in result.errors)

    def test_garageband_process_skipped_with_force(self, tmp_path):
        provider, _ = _make_full_project(tmp_path)
        with patch("core.restore._garageband_process_running", return_value=True):
            with _patch_snapshot():
                result = restore(provider, "TestProject", 1, force=True)

        assert result.success


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------

class TestHappyPath:

    def test_restore_replaces_project_data(self, tmp_path):
        """live/ProjectData should become the content of snapshot 001."""
        provider, paths = _make_full_project(tmp_path, num_snapshots=2)
        expected = b"gnoS snapshot 1"

        with _patch_snapshot(next_index=3):
            result = restore(provider, "TestProject", 1, force=True)

        assert result.success
        assert paths.live_project_data("TestProject").read_bytes() == expected

    def test_restore_creates_confirmation_snapshot(self, tmp_path):
        provider, _ = _make_full_project(tmp_path, num_snapshots=2)

        with _patch_snapshot(next_index=3) as mock_snap:
            result = restore(provider, "TestProject", 1, force=True)

        assert result.success
        assert result.new_snapshot_index == 3
        mock_snap.assert_called_once()

        # Verify call used the real take_snapshot signature
        call_kwargs = mock_snap.call_args.kwargs
        assert call_kwargs["project_name"] == "TestProject"
        assert call_kwargs["milestone"] is None
        assert "1" in call_kwargs["message"]

    def test_dry_run_does_not_write(self, tmp_path):
        provider, paths = _make_full_project(tmp_path, num_snapshots=2)
        original = paths.live_project_data("TestProject").read_bytes()

        with _patch_snapshot() as mock_snap:
            result = restore(provider, "TestProject", 1, force=True, dry_run=True)

        assert result.success
        # Content must NOT have changed
        assert paths.live_project_data("TestProject").read_bytes() == original
        # take_snapshot must NOT have been called
        mock_snap.assert_not_called()
        assert any("dry" in w.lower() for w in result.warnings)

    def test_restore_to_earliest_snapshot(self, tmp_path):
        provider, _ = _make_full_project(tmp_path, num_snapshots=5)
        with _patch_snapshot(next_index=6):
            result = restore(provider, "TestProject", 1, force=True)
        assert result.success
        assert result.restored_snapshot_index == 1

    def test_restore_to_latest_snapshot_is_valid(self, tmp_path):
        """Restoring to the latest snapshot is allowed — acts as a reset."""
        provider, _ = _make_full_project(tmp_path, num_snapshots=3)
        with _patch_snapshot(next_index=4):
            result = restore(provider, "TestProject", 3, force=True)
        assert result.success

    def test_restore_leaves_no_temp_files(self, tmp_path):
        """No .bt_backup_ or .bt_restore_ temp files should linger."""
        provider, paths = _make_full_project(tmp_path, num_snapshots=2)
        with _patch_snapshot():
            restore(provider, "TestProject", 1, force=True)

        pd_dir = paths.live_project_data("TestProject").parent
        leftovers = list(pd_dir.glob(".bt_*"))
        assert leftovers == [], f"Unexpected temp files: {leftovers}"

    def test_confirmation_description_includes_target_description(self, tmp_path):
        """Confirmation snapshot message should embed the target snapshot's description."""
        provider, paths = _make_full_project(tmp_path, num_snapshots=2)
        # Give snapshot 001 a recognisable description
        meta_path = paths.snapshot_meta(1)
        meta = json.loads(meta_path.read_text())
        meta["description"] = "Verse done"
        meta_path.write_text(json.dumps(meta))

        with _patch_snapshot(next_index=3) as mock_snap:
            restore(provider, "TestProject", 1, force=True)

        message = mock_snap.call_args.kwargs.get("message", "")
        assert "Verse done" in message

    def test_confirmation_snapshot_failure_is_non_fatal(self, tmp_path):
        """If take_snapshot raises, the restore still reports success with a warning."""
        provider, _ = _make_full_project(tmp_path, num_snapshots=2)
        with patch("core.restore.take_snapshot", side_effect=RuntimeError("disk full")):
            result = restore(provider, "TestProject", 1, force=True)

        assert result.success
        assert result.new_snapshot_index is None
        assert any("snapshot" in w.lower() for w in result.warnings)

    def test_confirmation_snapshot_ok_false_is_non_fatal(self, tmp_path):
        """If take_snapshot returns ok=False, restore still reports success with a warning."""
        provider, _ = _make_full_project(tmp_path, num_snapshots=2)

        bad_result = FakeSnapResult(index=0)
        bad_result.ok = False
        bad_result.errors = ["some internal error"]

        with patch("core.restore.take_snapshot", return_value=bad_result):
            result = restore(provider, "TestProject", 1, force=True)

        assert result.success
        assert result.new_snapshot_index is None
        assert any("confirmation snapshot" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# Rollback test
# ---------------------------------------------------------------------------

class TestRollback:

    def test_rollback_on_write_failure(self, tmp_path):
        """If the atomic write fails, live ProjectData must be unchanged."""
        provider, paths = _make_full_project(tmp_path, num_snapshots=2)
        original = paths.live_project_data("TestProject").read_bytes()

        # Make shutil.copy2 fail on the second call (the restore write;
        # the first call is the backup copy).
        call_count = {"n": 0}
        real_copy2 = shutil.copy2

        def failing_copy2(src, dst, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise OSError("Simulated disk failure")
            return real_copy2(src, dst, **kwargs)

        with patch("core.restore.shutil.copy2", side_effect=failing_copy2):
            result = restore(provider, "TestProject", 1, force=True)

        assert not result.success
        assert any("Write failed" in e for e in result.errors)
        # Live ProjectData must be back to its original content
        assert paths.live_project_data("TestProject").read_bytes() == original


# ---------------------------------------------------------------------------
# Internal helper unit tests
# ---------------------------------------------------------------------------

class TestInternalHelpers:

    def test_load_target_description_present(self, tmp_path):
        provider, paths = _make_full_project(tmp_path, num_snapshots=1)
        result = _load_target_snapshot_description(paths, 1)
        assert result == "Snapshot 1"

    def test_load_target_description_missing_file(self, tmp_path):
        provider, paths = _make_full_project(tmp_path, num_snapshots=1)
        paths.snapshot_meta(1).unlink()
        result = _load_target_snapshot_description(paths, 1)
        assert result is None

    def test_garageband_lock_present_true(self, tmp_path):
        (tmp_path / ".lck").touch()
        assert _garageband_lock_present(tmp_path) is True

    def test_garageband_lock_absent(self, tmp_path):
        assert _garageband_lock_present(tmp_path) is False


# ---------------------------------------------------------------------------
# RestoreResult.__str__ tests
# ---------------------------------------------------------------------------

class TestRestoreResultStr:

    def test_success_str(self):
        r = RestoreResult(
            success=True,
            restored_snapshot_index=3,
            new_snapshot_index=4,
            project_root=Path("/fake/project"),
        )
        s = str(r)
        assert "003" in s
        assert "004" in s

    def test_success_str_no_new_snapshot(self):
        """dry_run path — no new_snapshot_index."""
        r = RestoreResult(
            success=True,
            restored_snapshot_index=2,
            new_snapshot_index=None,
            project_root=Path("/fake/project"),
            warnings=["Dry run — no changes written."],
        )
        s = str(r)
        assert "002" in s
        assert "Dry run" in s

    def test_failure_str(self):
        r = RestoreResult(
            success=False,
            restored_snapshot_index=3,
            new_snapshot_index=None,
            project_root=Path("/fake/project"),
            errors=["Something went wrong"],
        )
        s = str(r)
        assert "failed" in s.lower()
        assert "Something went wrong" in s
