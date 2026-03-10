"""
tests/test_project_ops.py — Increment 9

Full test coverage for core/project_ops.py.

All tests use tmp_path fixtures and build minimal project trees
directly. Never calls initialize().

Test taxonomy
─────────────
get_status:
  test_happy_path_with_snapshot
  test_no_snapshots_yet
  test_lock_state_locked
  test_lock_state_open
  test_unsaved_changes_detected
  test_no_unsaved_changes
  test_unsaved_changes_no_live_pd
  test_unsaved_changes_no_snapshot_pd
  test_missing_project_json
  test_missing_handoff_json_warns
  test_missing_meta_json_warns
  test_latest_snapshot_with_milestone

get_log:
  test_happy_path_reverse_chronological
  test_empty_project_no_snapshots
  test_log_with_milestones
  test_log_reverse_flag
  test_missing_project_json
  test_corrupt_meta_json_warns

add_collaborator:
  test_happy_path
  test_duplicate_identifier_errors
  test_missing_project_json
  test_collaborator_persisted_to_disk

remove_collaborator:
  test_happy_path
  test_not_found_warns
  test_cannot_remove_owner
  test_missing_project_json
  test_removal_persisted_to_disk

rename_project:
  test_happy_path
  test_sanitizes_name
  test_same_name_no_change
  test_collision_errors
  test_missing_project_folder
  test_missing_project_json
  test_project_json_updated_on_disk
  test_folder_renamed_on_disk

_sanitize_raw_name:
  test_removes_unsafe_chars
  test_normalizes_whitespace
  test_empty_becomes_untitled
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.models import (
    Collaborator,
    Handoff,
    LockState,
    MilestoneTag,
    Project,
    ProjectPaths,
    Snapshot,
    StorageProvider,
)
from core.init import write_json_atomic
from core.project_ops import (
    AddCollaboratorResult,
    LogEntry,
    LogResult,
    RemoveCollaboratorResult,
    RenameResult,
    StatusResult,
    add_collaborator,
    get_log,
    get_status,
    remove_collaborator,
    rename_project,
    _sanitize_raw_name,
)


# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────

OWNER_ID = "alice@example.com"
OWNER_NAME = "Alice"
PROJECT_NAME = "TestProject"


def make_file(path: Path, content: bytes = b"data") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _make_provider(tmp_path: Path) -> StorageProvider:
    root = tmp_path / "BandTracker"
    root.mkdir(parents=True, exist_ok=True)
    return StorageProvider.local(root)


def _make_project(
    tmp_path: Path,
    num_snapshots: int = 1,
    with_live_pd: bool = True,
    with_handoff: bool = True,
    collaborators: list[Collaborator] | None = None,
) -> tuple[StorageProvider, ProjectPaths]:
    """
    Build a minimal project on disk.
    Returns (provider, paths).
    """
    provider = _make_provider(tmp_path)
    project_root = provider.project_path(PROJECT_NAME)
    project_root.mkdir(parents=True, exist_ok=True)
    paths = ProjectPaths(project_root)

    # Collaborators list
    collabs = collaborators or [
        Collaborator(display_name=OWNER_NAME, identifier=OWNER_ID),
    ]

    # project.json
    project = Project(
        name=PROJECT_NAME,
        uuid="test-uuid-1234",
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        owner=OWNER_ID,
        collaborators=collabs,
        latest_snapshot=num_snapshots if num_snapshots > 0 else None,
        next_snapshot_index=num_snapshots + 1 if num_snapshots > 0 else 1,
    )
    write_json_atomic(paths.project_json, project.to_json())

    # handoff.json
    if with_handoff:
        handoff = Handoff.open()
        write_json_atomic(paths.handoff_json, handoff.to_json())

    # Snapshots
    for i in range(1, num_snapshots + 1):
        snap_dir = paths.snapshot(i)
        snap_dir.mkdir(parents=True, exist_ok=True)
        paths.snapshot_sidecar(i).mkdir(parents=True, exist_ok=True)

        snap = Snapshot(
            index=i,
            description=f"Snapshot {i} description",
            timestamp=datetime(2024, 1, i, 12, 0, 0, tzinfo=timezone.utc),
            author=OWNER_ID,
            milestone=None,
        )
        write_json_atomic(paths.snapshot_meta(i), snap.to_json())

        # ProjectData in each snapshot
        make_file(paths.snapshot_project_data(i), f"pd-content-v{i}".encode())

    # live/ ProjectData — matches latest snapshot by default
    if with_live_pd and num_snapshots > 0:
        make_file(
            paths.live_project_data(PROJECT_NAME),
            f"pd-content-v{num_snapshots}".encode(),
        )

    return provider, paths


# ─────────────────────────────────────────────────────────────
# _sanitize_raw_name
# ─────────────────────────────────────────────────────────────

class TestSanitizeRawName:
    def test_removes_unsafe_chars(self):
        assert _sanitize_raw_name("My/Song:Title") == "MySongTitle"

    def test_normalizes_whitespace(self):
        assert _sanitize_raw_name("My   Song") == "My Song"

    def test_empty_becomes_untitled(self):
        assert _sanitize_raw_name("///") == "Untitled Project"

    def test_preserves_allowed_chars(self):
        assert _sanitize_raw_name("Song's (Demo) - v2") == "Song's (Demo) - v2"

    def test_strips_leading_trailing_whitespace(self):
        assert _sanitize_raw_name("  Song  ") == "Song"


# ─────────────────────────────────────────────────────────────
# get_status
# ─────────────────────────────────────────────────────────────

class TestGetStatus:
    def test_happy_path_with_snapshot(self, tmp_path):
        _, paths = _make_project(tmp_path, num_snapshots=3)

        result = get_status(paths, PROJECT_NAME)

        assert result.ok
        assert result.project_name == PROJECT_NAME
        assert result.latest_snapshot_index == 3
        assert result.latest_snapshot_description == "Snapshot 3 description"
        assert result.latest_snapshot_author == OWNER_ID
        assert result.latest_snapshot_timestamp is not None
        assert result.has_unsaved_changes is False

    def test_no_snapshots_yet(self, tmp_path):
        _, paths = _make_project(tmp_path, num_snapshots=0, with_live_pd=False)

        result = get_status(paths, PROJECT_NAME)

        assert result.ok
        assert result.latest_snapshot_index is None
        assert result.latest_snapshot_description is None
        assert result.has_unsaved_changes is False

    def test_lock_state_locked(self, tmp_path):
        _, paths = _make_project(tmp_path)

        locked = Handoff(
            active_editor=OWNER_ID,
            since=datetime.now(timezone.utc),
            lock_state=LockState.LOCKED,
        )
        write_json_atomic(paths.handoff_json, locked.to_json())

        result = get_status(paths, PROJECT_NAME)

        assert result.ok
        assert result.lock_state == "locked"
        assert result.active_editor == OWNER_ID

    def test_lock_state_open(self, tmp_path):
        _, paths = _make_project(tmp_path)

        result = get_status(paths, PROJECT_NAME)

        assert result.ok
        assert result.lock_state == "open"
        assert result.active_editor is None

    def test_unsaved_changes_detected(self, tmp_path):
        _, paths = _make_project(tmp_path, num_snapshots=1)

        # Modify live ProjectData so it differs from snapshot
        make_file(
            paths.live_project_data(PROJECT_NAME),
            b"modified-content",
        )

        result = get_status(paths, PROJECT_NAME)

        assert result.ok
        assert result.has_unsaved_changes is True

    def test_no_unsaved_changes(self, tmp_path):
        _, paths = _make_project(tmp_path, num_snapshots=1)
        # Default: live PD matches snapshot PD

        result = get_status(paths, PROJECT_NAME)

        assert result.ok
        assert result.has_unsaved_changes is False

    def test_unsaved_changes_no_live_pd(self, tmp_path):
        """If live PD doesn't exist, treat as no unsaved changes."""
        _, paths = _make_project(tmp_path, num_snapshots=1, with_live_pd=False)

        result = get_status(paths, PROJECT_NAME)

        assert result.ok
        assert result.has_unsaved_changes is False

    def test_unsaved_changes_no_snapshot_pd(self, tmp_path):
        """If snapshot PD doesn't exist, treat as no unsaved changes."""
        _, paths = _make_project(tmp_path, num_snapshots=1)

        # Remove snapshot ProjectData
        paths.snapshot_project_data(1).unlink()

        result = get_status(paths, PROJECT_NAME)

        assert result.ok
        assert result.has_unsaved_changes is False

    def test_missing_project_json(self, tmp_path):
        _, paths = _make_project(tmp_path)
        paths.project_json.unlink()

        result = get_status(paths, PROJECT_NAME)

        assert not result.ok
        assert any("project.json" in e for e in result.errors)

    def test_missing_handoff_json_warns(self, tmp_path):
        _, paths = _make_project(tmp_path, with_handoff=False)

        result = get_status(paths, PROJECT_NAME)

        assert result.ok
        assert result.lock_state is None
        assert any("handoff" in w.lower() for w in result.warnings)

    def test_missing_meta_json_warns(self, tmp_path):
        """If latest snapshot's meta.json is missing, warn but succeed."""
        _, paths = _make_project(tmp_path, num_snapshots=1)
        paths.snapshot_meta(1).unlink()

        result = get_status(paths, PROJECT_NAME)

        assert result.ok
        assert result.latest_snapshot_index is None
        assert any("meta.json" in w.lower() for w in result.warnings)

    def test_latest_snapshot_with_milestone(self, tmp_path):
        _, paths = _make_project(tmp_path, num_snapshots=1)

        # Rewrite snapshot with milestone
        snap = Snapshot(
            index=1,
            description="Final mix",
            timestamp=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            author=OWNER_ID,
            milestone=MilestoneTag.FINAL_MIX,
        )
        write_json_atomic(paths.snapshot_meta(1), snap.to_json())

        result = get_status(paths, PROJECT_NAME)

        assert result.ok
        assert result.latest_snapshot_milestone == "final_mix"


# ─────────────────────────────────────────────────────────────
# get_log
# ─────────────────────────────────────────────────────────────

class TestGetLog:
    def test_happy_path_reverse_chronological(self, tmp_path):
        _, paths = _make_project(tmp_path, num_snapshots=3)

        result = get_log(paths)

        assert result.ok
        assert len(result.entries) == 3
        # Newest first
        assert result.entries[0].index == 3
        assert result.entries[1].index == 2
        assert result.entries[2].index == 1

    def test_empty_project_no_snapshots(self, tmp_path):
        _, paths = _make_project(tmp_path, num_snapshots=0, with_live_pd=False)

        result = get_log(paths)

        assert result.ok
        assert result.entries == []

    def test_log_entry_fields(self, tmp_path):
        _, paths = _make_project(tmp_path, num_snapshots=1)

        result = get_log(paths)

        assert result.ok
        assert len(result.entries) == 1
        entry = result.entries[0]
        assert entry.index == 1
        assert entry.description == "Snapshot 1 description"
        assert entry.author == OWNER_ID
        assert entry.timestamp is not None
        assert entry.milestone is None

    def test_log_with_milestones(self, tmp_path):
        _, paths = _make_project(tmp_path, num_snapshots=2)

        # Rewrite snapshot 2 with milestone
        snap = Snapshot(
            index=2,
            description="Locked arrangement",
            timestamp=datetime(2024, 1, 2, 12, 0, 0, tzinfo=timezone.utc),
            author=OWNER_ID,
            milestone=MilestoneTag.ARRANGEMENT_LOCK,
        )
        write_json_atomic(paths.snapshot_meta(2), snap.to_json())

        result = get_log(paths)

        assert result.ok
        milestoned = [e for e in result.entries if e.milestone is not None]
        assert len(milestoned) == 1
        assert milestoned[0].milestone == "arrangement_lock"

    def test_log_can_be_reversed(self, tmp_path):
        """Verify entries can be reversed (CLI handles the flag)."""
        _, paths = _make_project(tmp_path, num_snapshots=3)

        result = get_log(paths)
        reversed_entries = list(reversed(result.entries))

        assert reversed_entries[0].index == 1
        assert reversed_entries[-1].index == 3

    def test_missing_project_json(self, tmp_path):
        _, paths = _make_project(tmp_path)
        paths.project_json.unlink()

        result = get_log(paths)

        assert not result.ok
        assert any("project.json" in e for e in result.errors)

    def test_corrupt_meta_json_warns(self, tmp_path):
        _, paths = _make_project(tmp_path, num_snapshots=2)

        # Corrupt one meta.json
        paths.snapshot_meta(1).write_text("not json{{{")

        result = get_log(paths)

        assert result.ok
        assert len(result.entries) == 1
        assert result.entries[0].index == 2
        assert any("001" in w for w in result.warnings)


# ─────────────────────────────────────────────────────────────
# add_collaborator
# ─────────────────────────────────────────────────────────────

class TestAddCollaborator:
    def test_happy_path(self, tmp_path):
        _, paths = _make_project(tmp_path)

        result = add_collaborator(paths, "Maya", "maya@email.com")

        assert result.ok
        assert result.identifier == "maya@email.com"
        assert result.display_name == "Maya"

    def test_duplicate_identifier_errors(self, tmp_path):
        _, paths = _make_project(tmp_path)

        result = add_collaborator(paths, "Alice Again", OWNER_ID)

        assert not result.ok
        assert any("already exists" in e for e in result.errors)

    def test_missing_project_json(self, tmp_path):
        _, paths = _make_project(tmp_path)
        paths.project_json.unlink()

        result = add_collaborator(paths, "Maya", "maya@email.com")

        assert not result.ok
        assert any("project.json" in e for e in result.errors)

    def test_collaborator_persisted_to_disk(self, tmp_path):
        _, paths = _make_project(tmp_path)

        add_collaborator(paths, "Maya", "maya@email.com")

        # Re-read from disk
        project = Project.from_json(paths.project_json.read_text())
        assert project.get_collaborator("maya@email.com") is not None
        assert project.get_collaborator("maya@email.com").display_name == "Maya"
        assert len(project.collaborators) == 2

    def test_preserves_existing_collaborators(self, tmp_path):
        _, paths = _make_project(tmp_path)

        add_collaborator(paths, "Maya", "maya@email.com")
        add_collaborator(paths, "Jordan", "jordan@email.com")

        project = Project.from_json(paths.project_json.read_text())
        assert len(project.collaborators) == 3

    def test_preserves_other_project_fields(self, tmp_path):
        _, paths = _make_project(tmp_path, num_snapshots=2)

        add_collaborator(paths, "Maya", "maya@email.com")

        project = Project.from_json(paths.project_json.read_text())
        assert project.name == PROJECT_NAME
        assert project.latest_snapshot == 2
        assert project.owner == OWNER_ID


# ─────────────────────────────────────────────────────────────
# remove_collaborator
# ─────────────────────────────────────────────────────────────

class TestRemoveCollaborator:
    def test_happy_path(self, tmp_path):
        collabs = [
            Collaborator(display_name=OWNER_NAME, identifier=OWNER_ID),
            Collaborator(display_name="Maya", identifier="maya@email.com"),
        ]
        _, paths = _make_project(tmp_path, collaborators=collabs)

        result = remove_collaborator(paths, "maya@email.com")

        assert result.ok
        assert result.identifier == "maya@email.com"
        assert not result.warnings

    def test_not_found_warns(self, tmp_path):
        _, paths = _make_project(tmp_path)

        result = remove_collaborator(paths, "nobody@email.com")

        assert result.ok
        assert any("not found" in w for w in result.warnings)

    def test_cannot_remove_owner(self, tmp_path):
        _, paths = _make_project(tmp_path)

        result = remove_collaborator(paths, OWNER_ID)

        assert not result.ok
        assert any("owner" in e.lower() for e in result.errors)

    def test_missing_project_json(self, tmp_path):
        _, paths = _make_project(tmp_path)
        paths.project_json.unlink()

        result = remove_collaborator(paths, "maya@email.com")

        assert not result.ok
        assert any("project.json" in e for e in result.errors)

    def test_removal_persisted_to_disk(self, tmp_path):
        collabs = [
            Collaborator(display_name=OWNER_NAME, identifier=OWNER_ID),
            Collaborator(display_name="Maya", identifier="maya@email.com"),
        ]
        _, paths = _make_project(tmp_path, collaborators=collabs)

        remove_collaborator(paths, "maya@email.com")

        project = Project.from_json(paths.project_json.read_text())
        assert project.get_collaborator("maya@email.com") is None
        assert len(project.collaborators) == 1

    def test_preserves_other_collaborators(self, tmp_path):
        collabs = [
            Collaborator(display_name=OWNER_NAME, identifier=OWNER_ID),
            Collaborator(display_name="Maya", identifier="maya@email.com"),
            Collaborator(display_name="Jordan", identifier="jordan@email.com"),
        ]
        _, paths = _make_project(tmp_path, collaborators=collabs)

        remove_collaborator(paths, "maya@email.com")

        project = Project.from_json(paths.project_json.read_text())
        assert len(project.collaborators) == 2
        assert project.get_collaborator("jordan@email.com") is not None


# ─────────────────────────────────────────────────────────────
# rename_project
# ─────────────────────────────────────────────────────────────

class TestRenameProject:
    def test_happy_path(self, tmp_path):
        provider, _ = _make_project(tmp_path)

        result = rename_project(provider, PROJECT_NAME, "New Song Title")

        assert result.ok
        assert result.old_name == PROJECT_NAME
        assert result.new_name == "New Song Title"

    def test_sanitizes_name(self, tmp_path):
        provider, _ = _make_project(tmp_path)

        result = rename_project(provider, PROJECT_NAME, "Song/With:Bad<Chars>")

        assert result.ok
        assert result.new_name == "SongWithBadChars"

    def test_same_name_no_change(self, tmp_path):
        provider, _ = _make_project(tmp_path)

        result = rename_project(provider, PROJECT_NAME, PROJECT_NAME)

        assert result.ok
        assert any("same" in w.lower() for w in result.warnings)

    def test_collision_errors(self, tmp_path):
        provider, _ = _make_project(tmp_path)

        # Create another project folder
        other_path = provider.project_path("Existing Song")
        other_path.mkdir(parents=True, exist_ok=True)

        result = rename_project(provider, PROJECT_NAME, "Existing Song")

        assert not result.ok
        assert any("already exists" in e for e in result.errors)

    def test_missing_project_folder(self, tmp_path):
        provider = _make_provider(tmp_path)

        result = rename_project(provider, "Nonexistent", "New Name")

        assert not result.ok
        assert any("not found" in e.lower() for e in result.errors)

    def test_missing_project_json(self, tmp_path):
        provider, paths = _make_project(tmp_path)
        paths.project_json.unlink()

        result = rename_project(provider, PROJECT_NAME, "New Name")

        assert not result.ok
        assert any("project.json" in e for e in result.errors)

    def test_project_json_updated_on_disk(self, tmp_path):
        provider, _ = _make_project(tmp_path)

        rename_project(provider, PROJECT_NAME, "Renamed Song")

        new_path = provider.project_path("Renamed Song")
        new_paths = ProjectPaths(new_path)
        project = Project.from_json(new_paths.project_json.read_text())
        assert project.name == "Renamed Song"

    def test_folder_renamed_on_disk(self, tmp_path):
        provider, _ = _make_project(tmp_path)

        rename_project(provider, PROJECT_NAME, "Renamed Song")

        old_path = provider.project_path(PROJECT_NAME)
        new_path = provider.project_path("Renamed Song")
        assert not old_path.exists()
        assert new_path.exists()

    def test_preserves_project_contents(self, tmp_path):
        provider, paths = _make_project(tmp_path, num_snapshots=2)

        rename_project(provider, PROJECT_NAME, "Renamed Song")

        new_paths = ProjectPaths(provider.project_path("Renamed Song"))
        project = Project.from_json(new_paths.project_json.read_text())
        assert project.uuid == "test-uuid-1234"
        assert project.latest_snapshot == 2
        assert project.owner == OWNER_ID

    def test_empty_name_after_sanitization_uses_untitled(self, tmp_path):
        provider, _ = _make_project(tmp_path)

        result = rename_project(provider, PROJECT_NAME, "///")

        assert result.ok
        assert result.new_name == "Untitled Project"


# ─────────────────────────────────────────────────────────────
# Result dataclass serialization (JSON-safety check)
# ─────────────────────────────────────────────────────────────

class TestResultSerialization:
    """Verify result dataclasses can be serialized via dataclasses.asdict()."""

    def test_status_result_serializable(self, tmp_path):
        _, paths = _make_project(tmp_path, num_snapshots=1)
        result = get_status(paths, PROJECT_NAME)

        from dataclasses import asdict
        d = asdict(result)
        # Should not raise
        json.dumps(d)

    def test_log_result_serializable(self, tmp_path):
        _, paths = _make_project(tmp_path, num_snapshots=2)
        result = get_log(paths)

        from dataclasses import asdict
        d = asdict(result)
        json.dumps(d)

    def test_add_collaborator_result_serializable(self, tmp_path):
        _, paths = _make_project(tmp_path)
        result = add_collaborator(paths, "Maya", "maya@email.com")

        from dataclasses import asdict
        d = asdict(result)
        json.dumps(d)

    def test_remove_collaborator_result_serializable(self, tmp_path):
        _, paths = _make_project(tmp_path)
        result = remove_collaborator(paths, "nobody@email.com")

        from dataclasses import asdict
        d = asdict(result)
        json.dumps(d)

    def test_rename_result_serializable(self, tmp_path):
        provider, _ = _make_project(tmp_path)
        result = rename_project(provider, PROJECT_NAME, "New Name")

        from dataclasses import asdict
        d = asdict(result)
        json.dumps(d)
