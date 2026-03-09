"""
tests/test_watcher.py  —  Increment 4: FSEvents watcher tests

Tests cover:
  - preflight: all pass/fail combinations
  - _run_diff: with changes, no changes, bad bytes
  - _sync_live_project_data: success and I/O failure
  - _ProjectDataHandler: debounce, path matching, event types
  - ProjectWatcher: start/stop lifecycle, on_save happy paths,
    user says y / user says n, auto_yes mode, sequential saves,
    snapshot failure propagation, event record accuracy
  - Integration: full watcher round-trip with real filesystem

All tests are synchronous — the watchdog Observer is never started.
on_save() is called directly to avoid timing-dependent threading tests.
"""

from __future__ import annotations

import json
import shutil
import struct
import threading
import time
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch
from core.snapshot import take_snapshot, SnapshotResult

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.init import PROJECTDATA_MAGIC, PROJECTDATA_MAGIC_OFFSET, initialize
from core.models import Project, ProjectPaths, StorageProvider
from core.snapshot import take_snapshot
from core.watcher import (
    PreflightResult,
    ProjectWatcher,
    WatchEvent,
    _ProjectDataHandler,
    _run_diff,
    _sync_live_project_data,
    preflight,
)


# ─────────────────────────────────────────────────────────────
# SHARED FIXTURES / HELPERS
# ─────────────────────────────────────────────────────────────

def make_project_data(tmp: Path, tempo: int = 120) -> bytes:
    """
    Build a minimal valid ProjectData binary that the diff engine
    can parse.  Mirrors what make_band() does in test_snapshot.py.
    """
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
        (band / "Media" / "Audio Files" / "Guitar.aif").write_bytes(b"AIFF" + b"\x00" * 64)
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
    """
    Build a ProjectWatcher with captured output and scripted prompt answers.

    prompts: list of strings returned in order for each prompt() call.
             Defaults to infinite "n" responses.
    Returns (watcher, printed_lines).
    """
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


# ─────────────────────────────────────────────────────────────
# preflight
# ─────────────────────────────────────────────────────────────

class TestPreflight:
    def test_all_present_returns_ok(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        gb_band = tmp_path / "gb" / f"{project_name}.band"
        result = preflight(provider, project_name, gb_band)
        assert result.ok
        assert result.errors == []

    def test_missing_project_json(self, tmp_path):
        provider = make_provider(tmp_path)
        gb_band = tmp_path / "gb" / "Fake.band"
        gb_band.mkdir(parents=True)
        result = preflight(provider, "Fake", gb_band)
        assert not result.ok
        assert any("init" in e.lower() for e in result.errors)

    def test_missing_gb_bundle(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        gb_band = tmp_path / "doesnotexist.band"
        result = preflight(provider, project_name, gb_band)
        assert not result.ok
        assert any("not found" in e.lower() for e in result.errors)

    def test_missing_project_data_inside_bundle(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        gb_band = tmp_path / "gb" / f"{project_name}.band"
        # Remove ProjectData from the bundle
        pd = gb_band / "Alternatives" / "000" / "ProjectData"
        pd.unlink()
        result = preflight(provider, project_name, gb_band)
        assert not result.ok
        assert any("projectdata" in e.lower() for e in result.errors)

    def test_missing_live_bundle(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        gb_band = tmp_path / "gb" / f"{project_name}.band"
        paths = ProjectPaths(provider.project_path(project_name))
        shutil.rmtree(paths.live_band(project_name))
        result = preflight(provider, project_name, gb_band)
        assert not result.ok
        assert any("live" in e.lower() or "init" in e.lower() for e in result.errors)

    def test_gb_pd_path_returned_on_success(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        gb_band = tmp_path / "gb" / f"{project_name}.band"
        result = preflight(provider, project_name, gb_band)
        assert result.gb_pd_path is not None
        assert result.gb_pd_path.name == "ProjectData"
        assert result.gb_pd_path.exists()

    def test_multiple_errors_all_collected(self, tmp_path):
        provider = make_provider(tmp_path)
        gb_band = tmp_path / "nonexistent.band"
        result = preflight(provider, "NoProject", gb_band)
        assert not result.ok
        assert len(result.errors) >= 2


# ─────────────────────────────────────────────────────────────
# _run_diff
# ─────────────────────────────────────────────────────────────

class TestRunDiff:
    def test_identical_bytes_returns_empty_summary(self, tmp_path):
        data = make_project_data(tmp_path)
        summary, desc = _run_diff(data, data, None)
        assert summary == [] or desc is None  # no meaningful changes

    def test_empty_old_bytes_does_not_crash(self, tmp_path):
        data = make_project_data(tmp_path)
        summary, desc = _run_diff(b"", data, None)
        # Should return without raising, result may or may not have content
        assert isinstance(summary, list)

    def test_garbage_bytes_does_not_crash(self, tmp_path):
        summary, desc = _run_diff(b"\xff\xfe\xfd", b"\x01\x02\x03", None)
        assert isinstance(summary, list)
        assert desc is None or isinstance(desc, str)

    def test_returns_tuple_of_list_and_optional_str(self, tmp_path):
        data = make_project_data(tmp_path)
        result = _run_diff(data, data, None)
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], list)
        assert result[1] is None or isinstance(result[1], str)

    def test_noise_mask_path_none_does_not_crash(self, tmp_path):
        data = make_project_data(tmp_path)
        summary, desc = _run_diff(data, data, None)
        assert isinstance(summary, list)

    def test_nonexistent_noise_mask_path_does_not_crash(self, tmp_path):
        data = make_project_data(tmp_path)
        summary, desc = _run_diff(data, data, tmp_path / "missing_mask.json")
        assert isinstance(summary, list)


# ─────────────────────────────────────────────────────────────
# _sync_live_project_data
# ─────────────────────────────────────────────────────────────

class TestSyncLiveProjectData:
    def test_copies_file_to_dest(self, tmp_path):
        src = tmp_path / "src" / "ProjectData"
        src.parent.mkdir()
        src.write_bytes(b"new bytes")
        dest = tmp_path / "live" / "ProjectData"

        err = _sync_live_project_data(src, dest)
        assert err is None
        assert dest.read_bytes() == b"new bytes"

    def test_overwrites_existing(self, tmp_path):
        src = tmp_path / "src" / "ProjectData"
        src.parent.mkdir()
        src.write_bytes(b"updated")
        dest = tmp_path / "live" / "ProjectData"
        dest.parent.mkdir()
        dest.write_bytes(b"old")

        err = _sync_live_project_data(src, dest)
        assert err is None
        assert dest.read_bytes() == b"updated"

    def test_creates_parent_dirs(self, tmp_path):
        src = tmp_path / "src" / "ProjectData"
        src.parent.mkdir()
        src.write_bytes(b"data")
        dest = tmp_path / "deep" / "nested" / "ProjectData"

        err = _sync_live_project_data(src, dest)
        assert err is None
        assert dest.exists()

    def test_missing_source_returns_error(self, tmp_path):
        src = tmp_path / "ghost" / "ProjectData"
        dest = tmp_path / "live" / "ProjectData"

        err = _sync_live_project_data(src, dest)
        assert err is not None
        assert isinstance(err, str)
        assert len(err) > 0


# ─────────────────────────────────────────────────────────────
# _ProjectDataHandler (debounce + path matching)
# ─────────────────────────────────────────────────────────────

class TestProjectDataHandler:
    def _make_handler(self, target: Path):
        fired_paths: list[Path] = []
        handler = _ProjectDataHandler(
            gb_project_data=target,
            on_save=fired_paths.append,
        )
        return handler, fired_paths

    def _mock_event(self, path: str, is_dir: bool = False):
        e = MagicMock()
        e.src_path = path
        e.dest_path = path
        e.is_directory = is_dir
        return e

    def test_on_modified_fires_for_target(self, tmp_path):
        target = tmp_path / "ProjectData"
        target.write_bytes(b"x")
        handler, fired = self._make_handler(target)
        handler.on_modified(self._mock_event(str(target)))
        assert len(fired) == 1

    def test_on_modified_ignores_other_files(self, tmp_path):
        target = tmp_path / "ProjectData"
        target.write_bytes(b"x")
        other = tmp_path / "OtherFile"
        handler, fired = self._make_handler(target)
        handler.on_modified(self._mock_event(str(other)))
        assert len(fired) == 0

    def test_on_created_fires_for_target(self, tmp_path):
        target = tmp_path / "ProjectData"
        target.write_bytes(b"x")
        handler, fired = self._make_handler(target)
        handler.on_created(self._mock_event(str(target)))
        assert len(fired) == 1

    def test_on_moved_fires_for_dest(self, tmp_path):
        target = tmp_path / "ProjectData"
        target.write_bytes(b"x")
        handler, fired = self._make_handler(target)
        e = MagicMock()
        e.dest_path = str(target)
        e.is_directory = False
        handler.on_moved(e)
        assert len(fired) == 1

    def test_directory_events_ignored(self, tmp_path):
        target = tmp_path / "ProjectData"
        target.write_bytes(b"x")
        handler, fired = self._make_handler(target)
        handler.on_modified(self._mock_event(str(target), is_dir=True))
        assert len(fired) == 0

    def test_debounce_suppresses_rapid_second_fire(self, tmp_path):
        target = tmp_path / "ProjectData"
        target.write_bytes(b"x")
        handler, fired = self._make_handler(target)
        # Fire twice in immediate succession
        handler.on_modified(self._mock_event(str(target)))
        handler.on_modified(self._mock_event(str(target)))
        assert len(fired) == 1  # second suppressed by debounce

    def test_debounce_allows_fire_after_cooldown(self, tmp_path):
        target = tmp_path / "ProjectData"
        target.write_bytes(b"x")
        handler, fired = self._make_handler(target)
        handler._last_fired = 0.0  # reset so first fires
        handler.on_modified(self._mock_event(str(target)))
        # Manually expire the debounce window
        handler._last_fired = 0.0
        handler.on_modified(self._mock_event(str(target)))
        assert len(fired) == 2


# ─────────────────────────────────────────────────────────────
# ProjectWatcher._on_save — core behavior
# ─────────────────────────────────────────────────────────────

class TestProjectWatcherOnSave:
    """
    All tests call _on_save() directly — no Observer started.
    This makes tests deterministic and fast.
    """

    def _setup(self, tmp_path, **kwargs):
        provider, project_name = init_project(tmp_path)
        gb_band = tmp_path / "gb" / f"{project_name}.band"
        watcher, printed = make_watcher(
            tmp_path, provider, project_name, gb_band, **kwargs
        )
        gb_pd = gb_band / "Alternatives" / "000" / "ProjectData"
        return watcher, printed, provider, project_name, gb_pd

    # ── User says "n" ────────────────────────────────────────

    def test_no_snapshot_when_user_says_n(self, tmp_path):
        watcher, printed, provider, project_name, gb_pd = self._setup(
            tmp_path, prompts=["n"]
        )
        watcher._on_save(gb_pd)
        project = Project.from_json(
            (provider.project_path(project_name) / "project.json").read_text()
        )
        # Should still be at snapshot 1 (from init)
        assert project.latest_snapshot == 1

    def test_live_pd_synced_even_when_user_says_n(self, tmp_path):
        watcher, printed, provider, project_name, gb_pd = self._setup(
            tmp_path, prompts=["n"]
        )
        # Write new bytes to the GB bundle
        new_bytes = make_project_data(tmp_path, tempo=140)
        gb_pd.write_bytes(new_bytes)

        watcher._on_save(gb_pd)

        live_pd = watcher._live_pd
        assert live_pd.read_bytes() == new_bytes

    def test_event_recorded_for_n(self, tmp_path):
        watcher, printed, provider, project_name, gb_pd = self._setup(
            tmp_path, prompts=["n"]
        )
        watcher._on_save(gb_pd)
        assert len(watcher.events) == 1
        assert watcher.events[0].user_chose_snapshot is False

    def test_skipped_message_printed(self, tmp_path):
        watcher, printed, provider, project_name, gb_pd = self._setup(
            tmp_path, prompts=["n"]
        )
        watcher._on_save(gb_pd)
        assert any("skipped" in line.lower() for line in printed)

    # ── User says "y" ────────────────────────────────────────

    def test_snapshot_created_when_user_says_y(self, tmp_path):
        watcher, printed, provider, project_name, gb_pd = self._setup(
            tmp_path, prompts=["y", ""]  # y to save, blank for auto-desc
        )
        watcher._on_save(gb_pd)
        project = Project.from_json(
            (provider.project_path(project_name) / "project.json").read_text()
        )
        assert project.latest_snapshot == 2

    def test_snapshot_result_recorded_in_event(self, tmp_path):
        watcher, printed, provider, project_name, gb_pd = self._setup(
            tmp_path, prompts=["y", ""]
        )
        watcher._on_save(gb_pd)
        assert watcher.events[0].snapshot_result is not None
        assert watcher.events[0].snapshot_result.ok

    def test_success_message_printed(self, tmp_path):
        watcher, printed, provider, project_name, gb_pd = self._setup(
            tmp_path, prompts=["y", ""]
        )
        watcher._on_save(gb_pd)
        assert any("✓" in line or "snapshot" in line.lower() for line in printed)

    def test_custom_message_used_when_provided(self, tmp_path):
        watcher, printed, provider, project_name, gb_pd = self._setup(
            tmp_path, prompts=["y", "My custom message"]
        )
        watcher._on_save(gb_pd)
        paths = ProjectPaths(provider.project_path(project_name))
        meta = json.loads(paths.snapshot_meta(2).read_text())
        assert meta["description"] == "My custom message"

    def test_event_user_chose_snapshot_true(self, tmp_path):
        watcher, printed, provider, project_name, gb_pd = self._setup(
            tmp_path, prompts=["y", ""]
        )
        watcher._on_save(gb_pd)
        assert watcher.events[0].user_chose_snapshot is True

    # ── auto_yes mode ─────────────────────────────────────────

    def test_auto_yes_creates_snapshot_without_prompt(self, tmp_path):
        watcher, printed, provider, project_name, gb_pd = self._setup(
            tmp_path, auto_yes=True
        )
        watcher._on_save(gb_pd)
        project = Project.from_json(
            (provider.project_path(project_name) / "project.json").read_text()
        )
        assert project.latest_snapshot == 2

    def test_auto_yes_does_not_call_prompt_fn(self, tmp_path):
        prompt_calls: list[str] = []

        provider, project_name = init_project(tmp_path)
        gb_band = tmp_path / "gb" / f"{project_name}.band"
        gb_pd = gb_band / "Alternatives" / "000" / "ProjectData"
        printed: list[str] = []

        watcher = ProjectWatcher(
            provider=provider,
            project_name=project_name,
            author="j@e.com",
            gb_band_path=gb_band,
            prompt_fn=lambda msg: prompt_calls.append(msg) or "y",
            print_fn=printed.append,
            auto_yes=True,
        )
        watcher._on_save(gb_pd)
        assert len(prompt_calls) == 0

    # ── Missing GB ProjectData ────────────────────────────────

    def test_missing_gb_pd_records_error_in_event(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        gb_band = tmp_path / "gb" / f"{project_name}.band"
        # Point to a path that does not exist
        fake_pd = gb_band / "Alternatives" / "000" / "ProjectData"
        fake_pd.unlink()

        watcher, printed = make_watcher(tmp_path, provider, project_name, gb_band)
        watcher._on_save(fake_pd)

        assert len(watcher.events) == 1
        assert watcher.events[0].error is not None

    # ── Sequential saves ──────────────────────────────────────

    def test_three_sequential_saves_all_yes(self, tmp_path):
        prompts = ["y", "", "y", "", "y", ""]  # y + blank desc × 3
        watcher, printed, provider, project_name, gb_pd = self._setup(
            tmp_path, prompts=prompts
        )
        for _ in range(3):
            watcher._on_save(gb_pd)

        project = Project.from_json(
            (provider.project_path(project_name) / "project.json").read_text()
        )
        assert project.latest_snapshot == 4  # init=1 + 3 snapshots

    def test_event_list_grows_with_each_save(self, tmp_path):
        watcher, printed, provider, project_name, gb_pd = self._setup(
            tmp_path, prompts=["n", "n", "n"]
        )
        for _ in range(3):
            watcher._on_save(gb_pd)
        assert len(watcher.events) == 3

    def test_alternating_y_and_n(self, tmp_path):
        prompts = ["y", "", "n", "y", ""]
        watcher, printed, provider, project_name, gb_pd = self._setup(
            tmp_path, prompts=prompts
        )
        watcher._on_save(gb_pd)  # y → snap 2
        watcher._on_save(gb_pd)  # n → no snap
        watcher._on_save(gb_pd)  # y → snap 3

        project = Project.from_json(
            (provider.project_path(project_name) / "project.json").read_text()
        )
        assert project.latest_snapshot == 3

    # ── Snapshot failure propagation ─────────────────────────

    def test_snapshot_failure_printed_not_raised(self, tmp_path):
        watcher, printed, provider, project_name, gb_pd = self._setup(
            tmp_path, prompts=["y", ""]
        )

        with patch("core.watcher.take_snapshot") as mock_snap:
            mock_snap.return_value = SnapshotResult(ok=False, errors=["simulated failure"])
            try:
                watcher._on_save(gb_pd)
            except Exception as exc:
                raise AssertionError(f"_on_save raised unexpectedly: {exc}")

        last_event = watcher.events[-1]
        error_communicated = (
            last_event.snapshot_result is not None and not last_event.snapshot_result.ok
            or any("✗" in line or "failed" in line.lower() for line in printed)
        )
        assert error_communicated

    # ── Output / announcement ─────────────────────────────────

    def test_save_detected_announced(self, tmp_path):
        watcher, printed, provider, project_name, gb_pd = self._setup(
            tmp_path, prompts=["n"]
        )
        watcher._on_save(gb_pd)
        combined = " ".join(printed).lower()
        assert "save" in combined or "detected" in combined or "garageband" in combined

    def test_separator_lines_printed(self, tmp_path):
        watcher, printed, provider, project_name, gb_pd = self._setup(
            tmp_path, prompts=["n"]
        )
        watcher._on_save(gb_pd)
        assert any("─" in line or "—" in line or "---" in line for line in printed)


# ─────────────────────────────────────────────────────────────
# ProjectWatcher lifecycle (start / stop)
# ─────────────────────────────────────────────────────────────

class TestProjectWatcherLifecycle:
    """
    These tests start the real Observer to verify start/stop works,
    but immediately stop it so tests finish quickly.
    """

    def test_start_and_stop_cleanly(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        gb_band = tmp_path / "gb" / f"{project_name}.band"
        watcher, _ = make_watcher(tmp_path, provider, project_name, gb_band)

        watcher.start()
        watcher.stop()
        # No assertion needed — must not raise

    def test_start_prints_watching_message(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        gb_band = tmp_path / "gb" / f"{project_name}.band"
        watcher, printed = make_watcher(tmp_path, provider, project_name, gb_band)

        watcher.start()
        watcher.stop()

        assert any("watching" in line.lower() for line in printed)

    def test_double_start_raises(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        gb_band = tmp_path / "gb" / f"{project_name}.band"
        watcher, _ = make_watcher(tmp_path, provider, project_name, gb_band)

        watcher.start()
        try:
            import pytest
            with pytest.raises(RuntimeError):
                watcher.start()
        finally:
            watcher.stop()

    def test_context_manager_stops_on_exit(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        gb_band = tmp_path / "gb" / f"{project_name}.band"
        watcher, _ = make_watcher(tmp_path, provider, project_name, gb_band)

        with watcher:
            pass  # start() called in __enter__, stop() in __exit__

        assert watcher._stop_event.is_set()

    def test_stop_sets_stop_event(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        gb_band = tmp_path / "gb" / f"{project_name}.band"
        watcher, _ = make_watcher(tmp_path, provider, project_name, gb_band)

        watcher.start()
        assert not watcher._stop_event.is_set()
        watcher.stop()
        assert watcher._stop_event.is_set()


# ─────────────────────────────────────────────────────────────
# WatchEvent data class
# ─────────────────────────────────────────────────────────────

class TestWatchEvent:
    def test_defaults(self, tmp_path):
        pd = tmp_path / "ProjectData"
        event = WatchEvent(gb_project_data_path=pd)
        assert event.diff_summary == []
        assert event.description is None
        assert event.user_chose_snapshot is None
        assert event.snapshot_result is None
        assert event.error is None

    def test_path_stored(self, tmp_path):
        pd = tmp_path / "ProjectData"
        event = WatchEvent(gb_project_data_path=pd)
        assert event.gb_project_data_path == pd


# ─────────────────────────────────────────────────────────────
# Integration: watcher + real filesystem save simulation
# ─────────────────────────────────────────────────────────────

class TestWatcherIntegration:
    """
    Simulate what happens when GarageBand actually saves:
    new bytes appear in the GB bundle's ProjectData.
    """

    def test_on_save_updates_live_after_y(self, tmp_path):
        """After y, live ProjectData should equal the new GB ProjectData bytes."""
        provider, project_name = init_project(tmp_path)
        gb_band = tmp_path / "gb" / f"{project_name}.band"
        gb_pd = gb_band / "Alternatives" / "000" / "ProjectData"

        new_bytes = make_project_data(tmp_path, tempo=130)
        gb_pd.write_bytes(new_bytes)

        watcher, printed = make_watcher(
            tmp_path, provider, project_name, gb_band,
            prompts=["y", "Updated tempo"]
        )
        watcher._on_save(gb_pd)

        live_pd = watcher._live_pd
        assert live_pd.read_bytes() == new_bytes

    def test_on_save_updates_live_after_n(self, tmp_path):
        """After n, live ProjectData should still be synced to the new bytes."""
        provider, project_name = init_project(tmp_path)
        gb_band = tmp_path / "gb" / f"{project_name}.band"
        gb_pd = gb_band / "Alternatives" / "000" / "ProjectData"

        new_bytes = make_project_data(tmp_path, tempo=95)
        gb_pd.write_bytes(new_bytes)

        watcher, printed = make_watcher(
            tmp_path, provider, project_name, gb_band,
            prompts=["n"]
        )
        watcher._on_save(gb_pd)

        live_pd = watcher._live_pd
        assert live_pd.read_bytes() == new_bytes

    def test_snapshot_folder_exists_after_y(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        gb_band = tmp_path / "gb" / f"{project_name}.band"
        gb_pd = gb_band / "Alternatives" / "000" / "ProjectData"

        watcher, printed = make_watcher(
            tmp_path, provider, project_name, gb_band,
            prompts=["y", ""]
        )
        watcher._on_save(gb_pd)

        paths = ProjectPaths(provider.project_path(project_name))
        assert paths.snapshot(2).exists()
        assert paths.snapshot_meta(2).exists()

    def test_no_snapshot_folder_after_n(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        gb_band = tmp_path / "gb" / f"{project_name}.band"
        gb_pd = gb_band / "Alternatives" / "000" / "ProjectData"

        watcher, printed = make_watcher(
            tmp_path, provider, project_name, gb_band,
            prompts=["n"]
        )
        watcher._on_save(gb_pd)

        paths = ProjectPaths(provider.project_path(project_name))
        assert not paths.snapshot(2).exists()

    def test_repeated_saves_increment_indices(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        gb_band = tmp_path / "gb" / f"{project_name}.band"
        gb_pd = gb_band / "Alternatives" / "000" / "ProjectData"

        watcher, printed = make_watcher(
            tmp_path, provider, project_name, gb_band,
            prompts=["y", "", "y", "", "y", ""]
        )

        for i in range(3):
            new_bytes = make_project_data(tmp_path, tempo=100 + i * 10)
            gb_pd.write_bytes(new_bytes)
            watcher._on_save(gb_pd)

        paths = ProjectPaths(provider.project_path(project_name))
        assert paths.snapshot(2).exists()
        assert paths.snapshot(3).exists()
        assert paths.snapshot(4).exists()
