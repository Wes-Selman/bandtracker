"""
tests/test_handoff.py

Full test coverage for Increment 7 — handoff state machine.

Tests:
  - do_handoff(): happy path, unknown recipient, already locked, force override,
                  handoff to self, note stored, snapshot_index set
  - do_release(): happy path, already open (idempotent), locked to other (error),
                  locked to other with force, lock_state written correctly
  - do_claim():   happy path, already claimed by self (warning), already claimed
                  by other (error), force override, unknown author
  - Watcher:      handoff.json change → current_handoff updated silently,
                  handoff_changes log populated, no terminal output
  - State machine: full transition sequence (claim → handoff → release → claim)
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.handoff_ops import do_claim, do_handoff, do_release
from core.models import (
    Collaborator,
    Handoff,
    LockState,
    Project,
    ProjectPaths,
    StorageProvider,
)
from core.init import write_json_atomic


# ─────────────────────────────────────────────────────────────
# FIXTURES
# ─────────────────────────────────────────────────────────────

OWNER_ID = "alice@example.com"
OWNER_NAME = "Alice"
COLLAB_ID = "maya@example.com"
COLLAB_NAME = "Maya"
STRANGER_ID = "stranger@example.com"
PROJECT_NAME = "TestProject"


def _make_project(tmp_path: Path) -> tuple[StorageProvider, ProjectPaths]:
    """
    Build a minimal but complete project on disk:
      - project.json with owner + one collaborator
      - handoff.json in open state
    Returns (provider, paths).
    """
    root = tmp_path / "BandTracker"
    provider = StorageProvider.local(root)
    project_root = provider.project_path(PROJECT_NAME)
    project_root.mkdir(parents=True)
    paths = ProjectPaths(project_root)

    owner = Collaborator(display_name=OWNER_NAME, identifier=OWNER_ID)
    collab = Collaborator(display_name=COLLAB_NAME, identifier=COLLAB_ID)

    project = Project(
        name=PROJECT_NAME,
        uuid="test-uuid-1234",
        created_at=datetime.now(timezone.utc),
        owner=OWNER_ID,
        collaborators=[owner, collab],
        latest_snapshot=3,
        next_snapshot_index=4,
    )
    write_json_atomic(paths.project_json, project.to_json())
    write_json_atomic(paths.handoff_json, Handoff.open().to_json())

    return provider, paths


def _read_handoff(paths: ProjectPaths) -> Handoff:
    return Handoff.from_json(paths.handoff_json.read_text())


# ─────────────────────────────────────────────────────────────
# do_handoff — happy path
# ─────────────────────────────────────────────────────────────

class TestDoHandoff:

    def test_happy_path_open_to_locked(self, tmp_path):
        provider, paths = _make_project(tmp_path)
        result = do_handoff(
            provider=provider,
            project_name=PROJECT_NAME,
            author=OWNER_ID,
            to_identifier=COLLAB_ID,
        )
        assert result.ok
        assert not result.errors

        h = _read_handoff(paths)
        assert h.lock_state == LockState.LOCKED
        assert h.active_editor == COLLAB_ID
        assert h.snapshot_index == 3  # project.latest_snapshot

    def test_note_stored(self, tmp_path):
        provider, paths = _make_project(tmp_path)
        result = do_handoff(
            provider=provider,
            project_name=PROJECT_NAME,
            author=OWNER_ID,
            to_identifier=COLLAB_ID,
            note="Bridge needs work",
        )
        assert result.ok
        h = _read_handoff(paths)
        assert h.note == "Bridge needs work"

    def test_summary_includes_recipient_name(self, tmp_path):
        provider, paths = _make_project(tmp_path)
        result = do_handoff(
            provider=provider,
            project_name=PROJECT_NAME,
            author=OWNER_ID,
            to_identifier=COLLAB_ID,
        )
        assert result.ok
        assert COLLAB_NAME in result.summary
        assert COLLAB_ID in result.summary

    def test_summary_includes_note_when_present(self, tmp_path):
        provider, paths = _make_project(tmp_path)
        result = do_handoff(
            provider=provider,
            project_name=PROJECT_NAME,
            author=OWNER_ID,
            to_identifier=COLLAB_ID,
            note="Check the chorus",
        )
        assert "Check the chorus" in result.summary

    def test_previous_state_captured(self, tmp_path):
        provider, paths = _make_project(tmp_path)
        result = do_handoff(
            provider=provider,
            project_name=PROJECT_NAME,
            author=OWNER_ID,
            to_identifier=COLLAB_ID,
        )
        assert result.previous_state is not None
        assert result.previous_state.lock_state == LockState.OPEN
        assert result.new_state is not None
        assert result.new_state.lock_state == LockState.LOCKED

    def test_handoff_to_self_warns(self, tmp_path):
        provider, paths = _make_project(tmp_path)
        result = do_handoff(
            provider=provider,
            project_name=PROJECT_NAME,
            author=OWNER_ID,
            to_identifier=OWNER_ID,  # handing off to self
        )
        assert result.ok
        assert any("yourself" in w.lower() for w in result.warnings)
        h = _read_handoff(paths)
        assert h.active_editor == OWNER_ID
        assert h.lock_state == LockState.LOCKED

    def test_unknown_recipient_errors(self, tmp_path):
        provider, paths = _make_project(tmp_path)
        result = do_handoff(
            provider=provider,
            project_name=PROJECT_NAME,
            author=OWNER_ID,
            to_identifier=STRANGER_ID,
        )
        assert not result.ok
        assert any("not a collaborator" in e for e in result.errors)

    def test_already_locked_errors_without_force(self, tmp_path):
        provider, paths = _make_project(tmp_path)
        # Pre-lock to owner
        locked = Handoff(
            active_editor=COLLAB_ID,
            since=datetime.now(timezone.utc),
            lock_state=LockState.LOCKED,
        )
        write_json_atomic(paths.handoff_json, locked.to_json())

        result = do_handoff(
            provider=provider,
            project_name=PROJECT_NAME,
            author=OWNER_ID,
            to_identifier=COLLAB_ID,
        )
        assert not result.ok
        assert any("already locked" in e for e in result.errors)
        assert any("--force" in e for e in result.errors)

    def test_already_locked_force_overrides(self, tmp_path):
        provider, paths = _make_project(tmp_path)
        locked = Handoff(
            active_editor=COLLAB_ID,
            since=datetime.now(timezone.utc),
            lock_state=LockState.LOCKED,
        )
        write_json_atomic(paths.handoff_json, locked.to_json())

        result = do_handoff(
            provider=provider,
            project_name=PROJECT_NAME,
            author=OWNER_ID,
            to_identifier=COLLAB_ID,
            force=True,
        )
        assert result.ok
        assert any("overrid" in w.lower() for w in result.warnings)
        h = _read_handoff(paths)
        assert h.active_editor == COLLAB_ID
        assert h.lock_state == LockState.LOCKED

    def test_missing_project_json_errors(self, tmp_path):
        provider, paths = _make_project(tmp_path)
        paths.project_json.unlink()
        result = do_handoff(
            provider=provider,
            project_name=PROJECT_NAME,
            author=OWNER_ID,
            to_identifier=COLLAB_ID,
        )
        assert not result.ok
        assert any("project.json" in e for e in result.errors)

    def test_missing_handoff_json_errors(self, tmp_path):
        provider, paths = _make_project(tmp_path)
        paths.handoff_json.unlink()
        result = do_handoff(
            provider=provider,
            project_name=PROJECT_NAME,
            author=OWNER_ID,
            to_identifier=COLLAB_ID,
        )
        assert not result.ok
        assert any("handoff.json" in e for e in result.errors)


# ─────────────────────────────────────────────────────────────
# do_release
# ─────────────────────────────────────────────────────────────

class TestDoRelease:

    def test_happy_path_locked_to_open(self, tmp_path):
        provider, paths = _make_project(tmp_path)
        # Pre-lock to owner
        locked = Handoff(
            active_editor=OWNER_ID,
            since=datetime.now(timezone.utc),
            lock_state=LockState.LOCKED,
            snapshot_index=3,
        )
        write_json_atomic(paths.handoff_json, locked.to_json())

        result = do_release(
            provider=provider,
            project_name=PROJECT_NAME,
            author=OWNER_ID,
        )
        assert result.ok
        assert not result.errors

        h = _read_handoff(paths)
        assert h.lock_state == LockState.OPEN
        assert h.active_editor is None
        assert h.note is None

    def test_already_open_is_idempotent(self, tmp_path):
        provider, paths = _make_project(tmp_path)
        # handoff.json starts open

        result = do_release(
            provider=provider,
            project_name=PROJECT_NAME,
            author=OWNER_ID,
        )
        assert result.ok
        assert any("already open" in w.lower() for w in result.warnings)
        # State unchanged
        h = _read_handoff(paths)
        assert h.lock_state == LockState.OPEN

    def test_locked_to_other_errors_without_force(self, tmp_path):
        provider, paths = _make_project(tmp_path)
        locked = Handoff(
            active_editor=COLLAB_ID,
            since=datetime.now(timezone.utc),
            lock_state=LockState.LOCKED,
        )
        write_json_atomic(paths.handoff_json, locked.to_json())

        result = do_release(
            provider=provider,
            project_name=PROJECT_NAME,
            author=OWNER_ID,  # NOT the lock holder
        )
        assert not result.ok
        assert any("locked to" in e for e in result.errors)
        assert any("--force" in e for e in result.errors)
        # File unchanged
        h = _read_handoff(paths)
        assert h.lock_state == LockState.LOCKED

    def test_locked_to_other_force_releases(self, tmp_path):
        provider, paths = _make_project(tmp_path)
        locked = Handoff(
            active_editor=COLLAB_ID,
            since=datetime.now(timezone.utc),
            lock_state=LockState.LOCKED,
        )
        write_json_atomic(paths.handoff_json, locked.to_json())

        result = do_release(
            provider=provider,
            project_name=PROJECT_NAME,
            author=OWNER_ID,
            force=True,
        )
        assert result.ok
        assert any(COLLAB_ID in w for w in result.warnings)

        h = _read_handoff(paths)
        assert h.lock_state == LockState.OPEN
        assert h.active_editor is None

    def test_summary_present_on_success(self, tmp_path):
        provider, paths = _make_project(tmp_path)
        locked = Handoff(
            active_editor=OWNER_ID,
            since=datetime.now(timezone.utc),
            lock_state=LockState.LOCKED,
        )
        write_json_atomic(paths.handoff_json, locked.to_json())

        result = do_release(
            provider=provider,
            project_name=PROJECT_NAME,
            author=OWNER_ID,
        )
        assert result.ok
        assert result.summary is not None
        assert len(result.summary) > 0

    def test_missing_project_json_errors(self, tmp_path):
        provider, paths = _make_project(tmp_path)
        paths.project_json.unlink()
        result = do_release(
            provider=provider,
            project_name=PROJECT_NAME,
            author=OWNER_ID,
        )
        assert not result.ok


# ─────────────────────────────────────────────────────────────
# do_claim
# ─────────────────────────────────────────────────────────────

class TestDoClaim:

    def test_happy_path_open_to_locked(self, tmp_path):
        provider, paths = _make_project(tmp_path)
        result = do_claim(
            provider=provider,
            project_name=PROJECT_NAME,
            author=OWNER_ID,
        )
        assert result.ok
        assert not result.errors

        h = _read_handoff(paths)
        assert h.lock_state == LockState.LOCKED
        assert h.active_editor == OWNER_ID

    def test_snapshot_index_set(self, tmp_path):
        provider, paths = _make_project(tmp_path)
        result = do_claim(
            provider=provider,
            project_name=PROJECT_NAME,
            author=OWNER_ID,
        )
        assert result.ok
        h = _read_handoff(paths)
        assert h.snapshot_index == 3  # project.latest_snapshot

    def test_claim_by_collab(self, tmp_path):
        provider, paths = _make_project(tmp_path)
        result = do_claim(
            provider=provider,
            project_name=PROJECT_NAME,
            author=COLLAB_ID,
        )
        assert result.ok
        h = _read_handoff(paths)
        assert h.active_editor == COLLAB_ID

    def test_already_claimed_by_self_warns_and_updates_timestamp(self, tmp_path):
        provider, paths = _make_project(tmp_path)
        # Pre-lock to owner
        locked = Handoff(
            active_editor=OWNER_ID,
            since=datetime(2020, 1, 1, tzinfo=timezone.utc),
            lock_state=LockState.LOCKED,
        )
        write_json_atomic(paths.handoff_json, locked.to_json())

        result = do_claim(
            provider=provider,
            project_name=PROJECT_NAME,
            author=OWNER_ID,
        )
        assert result.ok
        assert any("already claimed" in w.lower() for w in result.warnings)
        # Timestamp updated
        h = _read_handoff(paths)
        assert h.since > datetime(2020, 1, 1, tzinfo=timezone.utc)

    def test_already_claimed_by_other_errors_without_force(self, tmp_path):
        provider, paths = _make_project(tmp_path)
        locked = Handoff(
            active_editor=COLLAB_ID,
            since=datetime.now(timezone.utc),
            lock_state=LockState.LOCKED,
        )
        write_json_atomic(paths.handoff_json, locked.to_json())

        result = do_claim(
            provider=provider,
            project_name=PROJECT_NAME,
            author=OWNER_ID,
        )
        assert not result.ok
        assert any("already claimed" in e for e in result.errors)
        assert any("--force" in e for e in result.errors)

    def test_already_claimed_by_other_force_overrides(self, tmp_path):
        provider, paths = _make_project(tmp_path)
        locked = Handoff(
            active_editor=COLLAB_ID,
            since=datetime.now(timezone.utc),
            lock_state=LockState.LOCKED,
        )
        write_json_atomic(paths.handoff_json, locked.to_json())

        result = do_claim(
            provider=provider,
            project_name=PROJECT_NAME,
            author=OWNER_ID,
            force=True,
        )
        assert result.ok
        assert any("overrid" in w.lower() for w in result.warnings)

        h = _read_handoff(paths)
        assert h.active_editor == OWNER_ID
        assert h.lock_state == LockState.LOCKED

    def test_unknown_author_errors(self, tmp_path):
        provider, paths = _make_project(tmp_path)
        result = do_claim(
            provider=provider,
            project_name=PROJECT_NAME,
            author=STRANGER_ID,
        )
        assert not result.ok
        assert any("not a collaborator" in e for e in result.errors)

    def test_summary_includes_display_name(self, tmp_path):
        provider, paths = _make_project(tmp_path)
        result = do_claim(
            provider=provider,
            project_name=PROJECT_NAME,
            author=OWNER_ID,
        )
        assert result.ok
        assert OWNER_NAME in result.summary

    def test_missing_handoff_json_errors(self, tmp_path):
        provider, paths = _make_project(tmp_path)
        paths.handoff_json.unlink()
        result = do_claim(
            provider=provider,
            project_name=PROJECT_NAME,
            author=OWNER_ID,
        )
        assert not result.ok


# ─────────────────────────────────────────────────────────────
# Full state machine transitions
# ─────────────────────────────────────────────────────────────

class TestStateMachine:

    def test_full_cycle_claim_handoff_release_claim(self, tmp_path):
        """
        Walk through the canonical collaboration cycle:
          Open → Alice claims → Alice hands off to Maya →
          Maya releases → Bob... (just Alice again here) claims
        """
        provider, paths = _make_project(tmp_path)

        # 1. Alice claims
        r = do_claim(provider=provider, project_name=PROJECT_NAME, author=OWNER_ID)
        assert r.ok
        h = _read_handoff(paths)
        assert h.lock_state == LockState.LOCKED
        assert h.active_editor == OWNER_ID

        # 2. Alice hands off to Maya
        r = do_handoff(
            provider=provider,
            project_name=PROJECT_NAME,
            author=OWNER_ID,
            to_identifier=COLLAB_ID,
            note="Your turn on the bridge",
        )
        assert r.ok
        h = _read_handoff(paths)
        assert h.lock_state == LockState.LOCKED
        assert h.active_editor == COLLAB_ID
        assert h.note == "Your turn on the bridge"

        # 3. Maya releases (no one to hand back to specifically)
        r = do_release(provider=provider, project_name=PROJECT_NAME, author=COLLAB_ID)
        assert r.ok
        h = _read_handoff(paths)
        assert h.lock_state == LockState.OPEN
        assert h.active_editor is None

        # 4. Alice claims again
        r = do_claim(provider=provider, project_name=PROJECT_NAME, author=OWNER_ID)
        assert r.ok
        h = _read_handoff(paths)
        assert h.lock_state == LockState.LOCKED
        assert h.active_editor == OWNER_ID

    def test_handoff_locked_to_different_person_requires_force(self, tmp_path):
        """
        Maya has the ball. Alice tries to hand off to herself without --force.
        Should fail. Then succeeds with --force.
        """
        provider, paths = _make_project(tmp_path)

        # Maya claims first
        r = do_claim(provider=provider, project_name=PROJECT_NAME, author=COLLAB_ID)
        assert r.ok

        # Alice tries to claim without force — should fail
        r = do_claim(provider=provider, project_name=PROJECT_NAME, author=OWNER_ID)
        assert not r.ok

        # Alice claims with force
        r = do_claim(
            provider=provider,
            project_name=PROJECT_NAME,
            author=OWNER_ID,
            force=True,
        )
        assert r.ok
        h = _read_handoff(paths)
        assert h.active_editor == OWNER_ID

    def test_release_is_idempotent_multiple_calls(self, tmp_path):
        """Releasing an already-open project multiple times should be a no-op."""
        provider, paths = _make_project(tmp_path)

        for _ in range(3):
            r = do_release(
                provider=provider,
                project_name=PROJECT_NAME,
                author=OWNER_ID,
            )
            assert r.ok
            h = _read_handoff(paths)
            assert h.lock_state == LockState.OPEN


# ─────────────────────────────────────────────────────────────
# Watcher — handoff.json silent state update
# ─────────────────────────────────────────────────────────────

class TestWatcherHandoffDetection:
    """
    Tests for the _HandoffHandler integrated into ProjectWatcher.
    Uses _HandoffHandler directly to avoid needing a full watcher
    (which requires a GB bundle on disk, reconcile, etc.).
    """

    def test_handler_fires_on_file_write(self, tmp_path):
        """
        Write a new handoff.json — handler should parse it and call
        on_handoff_change with the new state.
        """
        from core.watcher import _HandoffHandler

        handoff_path = tmp_path / "handoff.json"
        write_json_atomic(handoff_path, Handoff.open().to_json())

        received: list[Handoff] = []
        handler = _HandoffHandler(
            handoff_json_path=handoff_path,
            on_handoff_change=received.append,
        )

        # Simulate a file write event directly
        new_handoff = Handoff(
            active_editor=COLLAB_ID,
            since=datetime.now(timezone.utc),
            lock_state=LockState.LOCKED,
            snapshot_index=5,
        )
        write_json_atomic(handoff_path, new_handoff.to_json())

        # Call _maybe_fire directly (bypass watchdog event system for unit test)
        handler._maybe_fire(str(handoff_path))

        assert len(received) == 1
        assert received[0].lock_state == LockState.LOCKED
        assert received[0].active_editor == COLLAB_ID
        assert received[0].snapshot_index == 5

    def test_handler_debounces_rapid_writes(self, tmp_path):
        """Two rapid _maybe_fire calls should only fire once."""
        from core.watcher import _HandoffHandler

        handoff_path = tmp_path / "handoff.json"
        write_json_atomic(handoff_path, Handoff.open().to_json())

        received: list[Handoff] = []
        handler = _HandoffHandler(
            handoff_json_path=handoff_path,
            on_handoff_change=received.append,
        )

        handler._maybe_fire(str(handoff_path))
        handler._maybe_fire(str(handoff_path))  # should be swallowed

        assert len(received) == 1

    def test_handler_ignores_unrelated_files(self, tmp_path):
        """Changes to other files in the same directory should be ignored."""
        from core.watcher import _HandoffHandler

        handoff_path = tmp_path / "handoff.json"
        write_json_atomic(handoff_path, Handoff.open().to_json())

        other_path = tmp_path / "project.json"
        other_path.write_text("{}")

        received: list[Handoff] = []
        handler = _HandoffHandler(
            handoff_json_path=handoff_path,
            on_handoff_change=received.append,
        )

        handler._maybe_fire(str(other_path))

        assert len(received) == 0

    def test_handler_tolerates_corrupt_file(self, tmp_path):
        """If handoff.json is corrupt mid-write, handler should not raise."""
        from core.watcher import _HandoffHandler

        handoff_path = tmp_path / "handoff.json"
        handoff_path.write_text("not valid json {{{")

        received: list[Handoff] = []
        handler = _HandoffHandler(
            handoff_json_path=handoff_path,
            on_handoff_change=received.append,
        )

        # Should not raise
        handler._maybe_fire(str(handoff_path))
        assert len(received) == 0

    def test_on_handoff_change_updates_watcher_state(self, tmp_path):
        """
        ProjectWatcher._on_handoff_change() should update current_handoff
        and append to handoff_changes without any terminal output.
        """
        # We test _on_handoff_change directly to avoid full watcher setup
        from core.watcher import ProjectWatcher

        root = tmp_path / "BandTracker"
        provider = StorageProvider.local(root)

        # Minimal watcher construction — don't call start()
        gb_band = tmp_path / "Test.band"
        gb_band.mkdir()

        printed: list[str] = []
        watcher = ProjectWatcher(
            provider=provider,
            project_name=PROJECT_NAME,
            author=OWNER_ID,
            gb_band_path=gb_band,
            print_fn=printed.append,
        )

        new_handoff = Handoff(
            active_editor=COLLAB_ID,
            since=datetime.now(timezone.utc),
            lock_state=LockState.LOCKED,
            snapshot_index=7,
        )
        watcher._on_handoff_change(new_handoff)

        assert watcher.current_handoff is not None
        assert watcher.current_handoff.active_editor == COLLAB_ID
        assert watcher.current_handoff.lock_state == LockState.LOCKED
        assert len(watcher.handoff_changes) == 1

        # Critically — no terminal output
        assert printed == []

    def test_handoff_changes_log_accumulates(self, tmp_path):
        """Multiple handoff changes should all be logged."""
        from core.watcher import ProjectWatcher

        root = tmp_path / "BandTracker"
        provider = StorageProvider.local(root)
        gb_band = tmp_path / "Test.band"
        gb_band.mkdir()

        watcher = ProjectWatcher(
            provider=provider,
            project_name=PROJECT_NAME,
            author=OWNER_ID,
            gb_band_path=gb_band,
        )

        states = [
            Handoff(active_editor=COLLAB_ID, since=datetime.now(timezone.utc),
                    lock_state=LockState.LOCKED),
            Handoff(active_editor=None, since=datetime.now(timezone.utc),
                    lock_state=LockState.OPEN),
            Handoff(active_editor=OWNER_ID, since=datetime.now(timezone.utc),
                    lock_state=LockState.LOCKED),
        ]

        for s in states:
            watcher._on_handoff_change(s)

        assert len(watcher.handoff_changes) == 3
        assert watcher.current_handoff.active_editor == OWNER_ID

    def test_on_handoff_change_is_thread_safe(self, tmp_path):
        """Concurrent calls to _on_handoff_change should not corrupt state."""
        from core.watcher import ProjectWatcher

        root = tmp_path / "BandTracker"
        provider = StorageProvider.local(root)
        gb_band = tmp_path / "Test.band"
        gb_band.mkdir()

        watcher = ProjectWatcher(
            provider=provider,
            project_name=PROJECT_NAME,
            author=OWNER_ID,
            gb_band_path=gb_band,
        )

        def write_changes(editor_id: str, count: int):
            for _ in range(count):
                h = Handoff(
                    active_editor=editor_id,
                    since=datetime.now(timezone.utc),
                    lock_state=LockState.LOCKED,
                )
                watcher._on_handoff_change(h)

        threads = [
            threading.Thread(target=write_changes, args=(OWNER_ID, 50)),
            threading.Thread(target=write_changes, args=(COLLAB_ID, 50)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All 100 changes logged, no corruption
        assert len(watcher.handoff_changes) == 100
        # current_handoff is one of the valid states (not None, not corrupt)
        assert watcher.current_handoff is not None
        assert watcher.current_handoff.active_editor in (OWNER_ID, COLLAB_ID)


# ─────────────────────────────────────────────────────────────
# HandoffResult shape
# ─────────────────────────────────────────────────────────────

class TestHandoffResultShape:
    """Verify HandoffResult fields are populated correctly in each case."""

    def test_failed_result_has_no_new_state(self, tmp_path):
        provider, paths = _make_project(tmp_path)
        result = do_handoff(
            provider=provider,
            project_name=PROJECT_NAME,
            author=OWNER_ID,
            to_identifier=STRANGER_ID,
        )
        assert not result.ok
        assert result.new_state is None
        assert result.summary is None

    def test_successful_result_has_both_states(self, tmp_path):
        provider, paths = _make_project(tmp_path)
        result = do_handoff(
            provider=provider,
            project_name=PROJECT_NAME,
            author=OWNER_ID,
            to_identifier=COLLAB_ID,
        )
        assert result.ok
        assert result.previous_state is not None
        assert result.new_state is not None
        assert result.summary is not None

    def test_warnings_list_is_always_present(self, tmp_path):
        provider, paths = _make_project(tmp_path)
        result = do_handoff(
            provider=provider,
            project_name=PROJECT_NAME,
            author=OWNER_ID,
            to_identifier=COLLAB_ID,
        )
        assert isinstance(result.warnings, list)
        assert isinstance(result.errors, list)
