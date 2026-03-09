"""
tests/test_reconcile.py  —  Increment 5: Reconciliation tests

Tests cover:
  - reconcile(): all ReconcileAction paths
      CLEAN      — GB bytes == snapshot bytes
      SKIPPED    — no snapshots yet (latest_snapshot is None)
      SNAPSHOTTED — user says y, take_snapshot() succeeds
      DEFERRED   — user says n
      ERROR      — missing project.json, missing GB ProjectData,
                   take_snapshot() failure
  - _run_diff(): identical bytes, empty baseline, garbage bytes,
                 noise mask handling
  - _read_bytes_safe(): happy path, missing file
  - _load_project(): happy path, missing file, corrupt JSON
  - Integration: reconcile wired into ProjectWatcher.start()
      - clean project → watcher starts without prompting
      - dirty project → watcher prompts before observer starts
      - no snapshots → watcher starts without prompting

All tests are synchronous.  The watchdog Observer is never started
(or is immediately stopped) so there are no timing dependencies.

Design note: reconcile() compares the GarageBand bundle (gb_band_path)
against the latest snapshot — NOT live/ vs snapshot. Tests that simulate
"offline edits" therefore write to the GB bundle's ProjectData, not live/.
"""

from __future__ import annotations

import json
import shutil
import struct
from pathlib import Path
from typing import Optional

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.init import PROJECTDATA_MAGIC, PROJECTDATA_MAGIC_OFFSET, initialize
from core.models import Project, ProjectPaths, StorageProvider
from core.snapshot import take_snapshot
from core.reconcile import (
    ReconcileAction,
    ReconcileResult,
    _load_project,
    _read_bytes_safe,
    _run_diff,
    reconcile,
)
from core.watcher import ProjectWatcher


# ─────────────────────────────────────────────────────────────
# SHARED FIXTURES / HELPERS
# ─────────────────────────────────────────────────────────────

def make_project_data(tmp: Path, tempo: int = 120) -> bytes:
    """Minimal valid ProjectData binary matching what test_watcher uses."""
    data = bytearray(512)
    data[PROJECTDATA_MAGIC_OFFSET:PROJECTDATA_MAGIC_OFFSET + 4] = PROJECTDATA_MAGIC
    struct.pack_into("<I", data, 0x40, tempo * 10_000)
    return bytes(data)


def make_band(tmp: Path, name: str = "TestProject",
              with_media: bool = False,
              tempo: int = 120) -> Path:
    """Minimal valid .band bundle at tmp/<name>.band"""
    band = tmp / f"{name}.band"
    (band / "Alternatives" / "000").mkdir(parents=True)
    (band / "Media" / "Audio Files").mkdir(parents=True)
    (band / "Alternatives" / "000" / "ProjectData").write_bytes(
        make_project_data(tmp, tempo=tempo)
    )
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


def make_watcher(
    tmp: Path,
    provider: StorageProvider,
    project_name: str,
    gb_band_path: Path,
    prompts: Optional[list[str]] = None,
    auto_yes: bool = False,
) -> tuple[ProjectWatcher, list[str]]:
    """Build a ProjectWatcher with captured output and scripted prompts."""
    printed: list[str] = []
    prompt_iter = iter(prompts or [])

    def prompt_fn(msg: str) -> str:
        try:
            return next(prompt_iter)
        except StopIteration:
            return "n"

    watcher = ProjectWatcher(
        provider=provider,
        project_name=project_name,
        author="j@e.com",
        gb_band_path=gb_band_path,
        prompt_fn=prompt_fn,
        print_fn=printed.append,
        auto_yes=auto_yes,
    )
    return watcher, printed


def gb_band_path(tmp: Path, project_name: str) -> Path:
    """Return the path to the GB bundle used by init_project."""
    return tmp / "gb" / f"{project_name}.band"


def dirty_gb_project_data(tmp: Path, project_name: str, tempo: int = 140) -> None:
    """
    Write new bytes into the GB bundle's ProjectData to simulate an offline
    edit — i.e. the musician used GarageBand while the watcher was off.

    reconcile() reads from the GB bundle, so this is what actually triggers
    drift detection. Writing to live/ has no effect on reconcile.
    """
    gb_pd = gb_band_path(tmp, project_name) / "Alternatives" / "000" / "ProjectData"
    gb_pd.write_bytes(make_project_data(tmp, tempo=tempo))


def capture_reconcile(
    provider: StorageProvider,
    project_name: str,
    prompts: Optional[list[str]] = None,
    author: str = "j@e.com",
    gb_path: Optional[Path] = None,
) -> tuple[ReconcileResult, list[str]]:
    """Call reconcile() with captured I/O and scripted prompt answers."""
    printed: list[str] = []
    prompt_iter = iter(prompts or [])

    def prompt_fn(msg: str) -> str:
        try:
            return next(prompt_iter)
        except StopIteration:
            return "n"

    result = reconcile(
        provider=provider,
        project_name=project_name,
        author=author,
        gb_band_path=gb_path,
        prompt_fn=prompt_fn,
        print_fn=printed.append,
    )
    return result, printed


# ─────────────────────────────────────────────────────────────
# _load_project
# ─────────────────────────────────────────────────────────────

class TestLoadProject:
    def test_happy_path(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        paths = ProjectPaths(provider.project_path(project_name))
        project, err = _load_project(paths)
        assert err == ""
        assert project is not None
        assert project.name == project_name

    def test_missing_project_json_returns_error(self, tmp_path):
        provider = make_provider(tmp_path)
        paths = ProjectPaths(provider.project_path("Ghost"))
        project, err = _load_project(paths)
        assert project is None
        assert "project.json" in err

    def test_corrupt_json_returns_error(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        paths = ProjectPaths(provider.project_path(project_name))
        paths.project_json.write_text("{not valid json")
        project, err = _load_project(paths)
        assert project is None
        assert len(err) > 0


# ─────────────────────────────────────────────────────────────
# _read_bytes_safe
# ─────────────────────────────────────────────────────────────

class TestReadBytesSafe:
    def test_reads_existing_file(self, tmp_path):
        f = tmp_path / "test.bin"
        f.write_bytes(b"\x01\x02\x03")
        data, err = _read_bytes_safe(f)
        assert err == ""
        assert data == b"\x01\x02\x03"

    def test_missing_file_returns_error(self, tmp_path):
        data, err = _read_bytes_safe(tmp_path / "ghost.bin")
        assert data is None
        assert "not found" in err.lower() or len(err) > 0

    def test_empty_file_returns_empty_bytes(self, tmp_path):
        f = tmp_path / "empty.bin"
        f.write_bytes(b"")
        data, err = _read_bytes_safe(f)
        assert err == ""
        assert data == b""


# ─────────────────────────────────────────────────────────────
# _run_diff
# ─────────────────────────────────────────────────────────────

class TestRunDiff:
    def test_identical_bytes_returns_empty(self, tmp_path):
        data = make_project_data(tmp_path)
        summary, desc = _run_diff(data, data, None)
        assert isinstance(summary, list)
        assert desc is None or isinstance(desc, str)

    def test_empty_old_bytes_does_not_crash(self, tmp_path):
        data = make_project_data(tmp_path)
        summary, desc = _run_diff(b"", data, None)
        assert isinstance(summary, list)

    def test_garbage_bytes_does_not_crash(self, tmp_path):
        summary, desc = _run_diff(b"\xff\xfe", b"\x01\x02", None)
        assert isinstance(summary, list)
        assert desc is None or isinstance(desc, str)

    def test_returns_tuple(self, tmp_path):
        data = make_project_data(tmp_path)
        result = _run_diff(data, data, None)
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_missing_noise_mask_path_does_not_crash(self, tmp_path):
        data = make_project_data(tmp_path)
        summary, desc = _run_diff(data, data, tmp_path / "missing.json")
        assert isinstance(summary, list)


# ─────────────────────────────────────────────────────────────
# reconcile() — SKIPPED (no snapshots)
# ─────────────────────────────────────────────────────────────

class TestReconcileSkipped:
    def test_skipped_when_no_snapshots(self, tmp_path):
        """
        Force latest_snapshot = None in project.json to simulate a project
        that was initialized but has no snapshots (edge case — init always
        takes snapshot 1, but we test the guard anyway).
        """
        provider, project_name = init_project(tmp_path)
        paths = ProjectPaths(provider.project_path(project_name))

        # Manually clear latest_snapshot
        project = Project.from_json(paths.project_json.read_text())
        project.latest_snapshot = None
        paths.project_json.write_text(project.to_json())

        result, printed = capture_reconcile(provider, project_name)

        assert result.ok
        assert result.action == ReconcileAction.SKIPPED

    def test_skipped_prints_nothing_disruptive(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        paths = ProjectPaths(provider.project_path(project_name))
        project = Project.from_json(paths.project_json.read_text())
        project.latest_snapshot = None
        paths.project_json.write_text(project.to_json())

        result, printed = capture_reconcile(provider, project_name)

        # SKIPPED is silent — no prompts, no noise
        assert not any("save" in line.lower() for line in printed)


# ─────────────────────────────────────────────────────────────
# reconcile() — CLEAN (no drift)
# ─────────────────────────────────────────────────────────────

class TestReconcileClean:
    def test_clean_when_bytes_match(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        # init takes snapshot 1 from the GB bundle — GB and snapshot match
        result, printed = capture_reconcile(provider, project_name)
        assert result.ok
        assert result.action == ReconcileAction.CLEAN

    def test_clean_does_not_prompt(self, tmp_path):
        prompts_consumed: list[str] = []
        provider, project_name = init_project(tmp_path)

        def tracking_prompt(msg: str) -> str:
            prompts_consumed.append(msg)
            return "n"

        reconcile(provider, project_name, "j@e.com",
                  prompt_fn=tracking_prompt, print_fn=lambda _: None)
        assert len(prompts_consumed) == 0

    def test_clean_sets_latest_snapshot_index(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        result, _ = capture_reconcile(provider, project_name)
        assert result.latest_snapshot_index == 1


# ─────────────────────────────────────────────────────────────
# reconcile() — DEFERRED (user says n)
# ─────────────────────────────────────────────────────────────

class TestReconcileDeferred:
    def test_deferred_when_user_says_n(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        dirty_gb_project_data(tmp_path, project_name, tempo=150)

        result, _ = capture_reconcile(provider, project_name, prompts=["n"])

        assert result.ok
        assert result.action == ReconcileAction.DEFERRED

    def test_deferred_does_not_create_snapshot(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        dirty_gb_project_data(tmp_path, project_name, tempo=150)

        capture_reconcile(provider, project_name, prompts=["n"])

        paths = ProjectPaths(provider.project_path(project_name))
        project = Project.from_json(paths.project_json.read_text())
        # Still at snapshot 1 from init
        assert project.latest_snapshot == 1

    def test_deferred_prints_drift_detected(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        dirty_gb_project_data(tmp_path, project_name, tempo=150)

        result, printed = capture_reconcile(provider, project_name, prompts=["n"])

        combined = " ".join(printed).lower()
        assert "unsaved" in combined or "changes" in combined or "detected" in combined

    def test_deferred_prints_continue_message(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        dirty_gb_project_data(tmp_path, project_name, tempo=150)

        result, printed = capture_reconcile(provider, project_name, prompts=["n"])

        combined = " ".join(printed).lower()
        assert "continuing" in combined or "snapshot" in combined or "live" in combined

    def test_deferred_sets_latest_snapshot_index(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        dirty_gb_project_data(tmp_path, project_name, tempo=150)

        result, _ = capture_reconcile(provider, project_name, prompts=["n"])

        assert result.latest_snapshot_index == 1

    def test_eofError_in_prompt_treated_as_n(self, tmp_path):
        """EOFError from prompt (e.g. piped input exhausted) defaults to n."""
        provider, project_name = init_project(tmp_path)
        dirty_gb_project_data(tmp_path, project_name, tempo=150)

        def eof_prompt(msg: str) -> str:
            raise EOFError

        result = reconcile(provider, project_name, "j@e.com",
                           prompt_fn=eof_prompt, print_fn=lambda _: None)

        assert result.ok
        assert result.action == ReconcileAction.DEFERRED


# ─────────────────────────────────────────────────────────────
# reconcile() — SNAPSHOTTED (user says y)
# ─────────────────────────────────────────────────────────────

class TestReconcileSnapshotted:
    def test_snapshotted_when_user_says_y(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        dirty_gb_project_data(tmp_path, project_name, tempo=160)

        result, _ = capture_reconcile(provider, project_name, prompts=["y"])

        assert result.ok
        assert result.action == ReconcileAction.SNAPSHOTTED

    def test_snapshotted_increments_snapshot_index(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        dirty_gb_project_data(tmp_path, project_name, tempo=160)

        capture_reconcile(provider, project_name, prompts=["y"])

        paths = ProjectPaths(provider.project_path(project_name))
        project = Project.from_json(paths.project_json.read_text())
        assert project.latest_snapshot == 2

    def test_snapshotted_result_carries_snapshot_result(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        dirty_gb_project_data(tmp_path, project_name, tempo=160)

        result, _ = capture_reconcile(provider, project_name, prompts=["y"])

        assert result.snapshot_result is not None
        assert result.snapshot_result.ok
        assert result.snapshot_result.snapshot_index == 2

    def test_snapshotted_snapshot_folder_exists(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        dirty_gb_project_data(tmp_path, project_name, tempo=160)

        capture_reconcile(provider, project_name, prompts=["y"])

        paths = ProjectPaths(provider.project_path(project_name))
        assert paths.snapshot(2).exists()
        assert paths.snapshot_meta(2).exists()

    def test_snapshotted_prints_success_message(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        dirty_gb_project_data(tmp_path, project_name, tempo=160)

        result, printed = capture_reconcile(provider, project_name, prompts=["y"])

        assert any("✓" in line or "snapshot" in line.lower() for line in printed)

    def test_snapshotted_sets_latest_snapshot_index_to_old(self, tmp_path):
        """latest_snapshot_index records the pre-reconcile index, not the new one."""
        provider, project_name = init_project(tmp_path)
        dirty_gb_project_data(tmp_path, project_name, tempo=160)

        result, _ = capture_reconcile(provider, project_name, prompts=["y"])

        assert result.latest_snapshot_index == 1  # was 1 before reconcile

    def test_snapshotted_then_clean_on_second_call(self, tmp_path):
        """After snapshotting, a second reconcile should see clean state."""
        provider, project_name = init_project(tmp_path)
        dirty_gb_project_data(tmp_path, project_name, tempo=160)

        capture_reconcile(provider, project_name, prompts=["y"])

        # GB bundle now matches snapshot 2 (live/ was synced from GB)
        result, _ = capture_reconcile(provider, project_name)
        assert result.ok
        assert result.action == ReconcileAction.CLEAN


# ─────────────────────────────────────────────────────────────
# reconcile() — ERROR paths
# ─────────────────────────────────────────────────────────────

class TestReconcileErrors:
    def test_error_when_project_json_missing(self, tmp_path):
        provider = make_provider(tmp_path)
        result, _ = capture_reconcile(provider, "NonExistent")

        assert not result.ok
        assert result.action == ReconcileAction.ERROR
        assert len(result.errors) > 0

    def test_error_when_gb_project_data_missing(self, tmp_path):
        """If the GB bundle's ProjectData can't be read, that's a hard error."""
        provider, project_name = init_project(tmp_path)
        # Delete ProjectData from the GB bundle
        gb_pd = gb_band_path(tmp_path, project_name) / "Alternatives" / "000" / "ProjectData"
        gb_pd.unlink()

        result, _ = capture_reconcile(provider, project_name)

        assert not result.ok
        assert result.action == ReconcileAction.ERROR
        assert any("garageband" in e.lower() or "projectdata" in e.lower()
                   for e in result.errors)

    def test_warning_when_snapshot_project_data_missing(self, tmp_path):
        """
        If the snapshot's ProjectData is missing (corrupted store),
        reconcile should issue a warning and treat as CLEAN rather than ERROR,
        so it doesn't block the watcher.
        """
        provider, project_name = init_project(tmp_path)
        paths = ProjectPaths(provider.project_path(project_name))
        paths.snapshot_project_data(1).unlink()

        result, _ = capture_reconcile(provider, project_name)

        # Should proceed (ok=True) with a warning, not block entirely
        assert result.ok
        assert result.action == ReconcileAction.CLEAN
        assert len(result.warnings) > 0

    def test_error_when_project_json_corrupt(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        paths = ProjectPaths(provider.project_path(project_name))
        paths.project_json.write_text("{ invalid json }")

        result, _ = capture_reconcile(provider, project_name)

        assert not result.ok
        assert result.action == ReconcileAction.ERROR


# ─────────────────────────────────────────────────────────────
# reconcile() — diff_summary and description populated on drift
# ─────────────────────────────────────────────────────────────

class TestReconcileDiffContent:
    def test_diff_summary_is_list_on_drift(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        dirty_gb_project_data(tmp_path, project_name, tempo=150)

        result, _ = capture_reconcile(provider, project_name, prompts=["n"])

        assert isinstance(result.diff_summary, list)

    def test_description_is_none_or_str_on_drift(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        dirty_gb_project_data(tmp_path, project_name, tempo=150)

        result, _ = capture_reconcile(provider, project_name, prompts=["n"])

        assert result.description is None or isinstance(result.description, str)

    def test_diff_displayed_when_drift_detected(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        dirty_gb_project_data(tmp_path, project_name, tempo=150)

        result, printed = capture_reconcile(provider, project_name, prompts=["n"])

        combined = " ".join(printed)
        assert len(combined) > 0

    def test_separator_printed_on_drift(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        dirty_gb_project_data(tmp_path, project_name, tempo=150)

        result, printed = capture_reconcile(provider, project_name, prompts=["n"])

        assert any("─" in line or "—" in line or "---" in line or "-" * 5 in line
                   for line in printed)


# ─────────────────────────────────────────────────────────────
# ReconcileResult dataclass
# ─────────────────────────────────────────────────────────────

class TestReconcileResult:
    def test_defaults(self):
        result = ReconcileResult(ok=True, action=ReconcileAction.CLEAN)
        assert result.errors == []
        assert result.warnings == []
        assert result.diff_summary == []
        assert result.description is None
        assert result.snapshot_result is None
        assert result.latest_snapshot_index is None

    def test_ok_false_for_error_action(self):
        result = ReconcileResult(ok=False, action=ReconcileAction.ERROR,
                                 errors=["something broke"])
        assert not result.ok
        assert result.action == ReconcileAction.ERROR


# ─────────────────────────────────────────────────────────────
# Integration: reconcile() wired into ProjectWatcher.start()
# ─────────────────────────────────────────────────────────────

class TestWatcherReconcileIntegration:
    """
    ProjectWatcher.start() should call reconcile() before the Observer
    begins. Tests verify the integration without starting the Observer.

    We call watcher.start() and immediately watcher.stop() to avoid
    actually blocking. The reconcile side-effects (printed output,
    snapshot creation) are observable synchronously.
    """

    def test_clean_project_starts_without_prompt(self, tmp_path):
        """Fresh init → GB matches snapshot 1 → no reconcile prompt."""
        provider, project_name = init_project(tmp_path)
        gb_band = gb_band_path(tmp_path, project_name)
        watcher, printed = make_watcher(tmp_path, provider, project_name, gb_band)

        watcher.start()
        watcher.stop()

        # No drift → no "unsaved changes" announcement
        combined = " ".join(printed).lower()
        assert "unsaved" not in combined

    def test_dirty_project_prompts_on_start(self, tmp_path):
        """Offline edit (GB dirtied) → watcher surfaces drift before watching begins."""
        provider, project_name = init_project(tmp_path)
        dirty_gb_project_data(tmp_path, project_name, tempo=180)

        gb_band = gb_band_path(tmp_path, project_name)
        watcher, printed = make_watcher(
            tmp_path, provider, project_name, gb_band, prompts=["n"]
        )

        watcher.start()
        watcher.stop()

        combined = " ".join(printed).lower()
        assert (
            "unsaved" in combined
            or "changes" in combined
            or "detected" in combined
        )

    def test_dirty_project_y_creates_snapshot_on_start(self, tmp_path):
        """User says y to reconcile → snapshot 2 created before watcher runs."""
        provider, project_name = init_project(tmp_path)
        dirty_gb_project_data(tmp_path, project_name, tempo=180)

        gb_band = gb_band_path(tmp_path, project_name)
        watcher, printed = make_watcher(
            tmp_path, provider, project_name, gb_band, prompts=["y"]
        )

        watcher.start()
        watcher.stop()

        paths = ProjectPaths(provider.project_path(project_name))
        project = Project.from_json(paths.project_json.read_text())
        assert project.latest_snapshot == 2

    def test_no_snapshots_project_starts_without_prompt(self, tmp_path):
        """No snapshots yet → SKIPPED → watcher starts silently."""
        provider, project_name = init_project(tmp_path)
        paths = ProjectPaths(provider.project_path(project_name))

        # Strip latest_snapshot
        project = Project.from_json(paths.project_json.read_text())
        project.latest_snapshot = None
        paths.project_json.write_text(project.to_json())

        gb_band = gb_band_path(tmp_path, project_name)
        prompt_calls: list[str] = []

        watcher = ProjectWatcher(
            provider=provider,
            project_name=project_name,
            author="j@e.com",
            gb_band_path=gb_band,
            prompt_fn=lambda msg: prompt_calls.append(msg) or "n",
            print_fn=lambda _: None,
        )

        watcher.start()
        watcher.stop()

        # No prompts should have been issued by reconcile
        assert len(prompt_calls) == 0

    def test_watching_message_printed_after_reconcile(self, tmp_path):
        """The 'Watching ...' message should appear even after reconcile runs."""
        provider, project_name = init_project(tmp_path)
        dirty_gb_project_data(tmp_path, project_name, tempo=180)

        gb_band = gb_band_path(tmp_path, project_name)
        watcher, printed = make_watcher(
            tmp_path, provider, project_name, gb_band, prompts=["n"]
        )

        watcher.start()
        watcher.stop()

        assert any("watching" in line.lower() for line in printed)
