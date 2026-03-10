"""
tests/test_sidecar.py — Increment 8: Sidecar document tests

Coverage:
  SidecarEntry / SidecarType
    - to_dict / from_dict round-trip
    - from_legacy promotes plain strings to VERSION

  Snapshot model (backward-compat)
    - Old list[str] sidecar_files round-trips correctly
    - New list[SidecarEntry] round-trips correctly
    - Mixed old/new JSON is handled

  do_attach
    - Happy path: version attachment, project attachment
    - Defaults to latest snapshot when --snapshot omitted
    - Explicit --snapshot targets the right snapshot
    - Missing source file → error
    - Source is a directory → error
    - Non-existent snapshot → error
    - Same filename on same snapshot → overwrite + warning
    - Different filename on same snapshot → both present
    - sidecar/ directory created if missing
    - meta.json updated atomically (no corruption check — trust write_json_atomic)
    - Attached file content matches source

  do_detach
    - Happy path: file removed, meta.json updated
    - Defaults to latest snapshot
    - Explicit snapshot
    - Filename not attached → error
    - Non-existent snapshot → error
    - File already missing from disk → warning, meta.json still updated
    - After detach, file is not in meta.json
    - After detach, file is not on disk

  list_attachments (resolved mode)
    - Empty project → empty list
    - Only VERSION entries on requested snapshot
    - PROJECT entries inherit across snapshots
    - PROJECT shadowing: later snapshot's file wins
    - Detaching later PROJECT entry reveals earlier one
    - VERSION entries from other snapshots not included
    - snapshot_index defaults to latest
    - Explicit snapshot_index

  list_attachments (--all mode)
    - Returns every entry across all snapshots flat
    - Sorted by (snapshot_index, type, filename)
    - Ignores snapshot_index argument

  resolve_attachments_at
    - Snapshot with both types returns correct items
    - PROJECT entry from snapshot 3 visible at snapshot 5
    - PROJECT entry from snapshot 3 shadowed by snapshot 7 at snapshot 7
    - VERSION entry from snapshot 3 NOT visible at snapshot 5

  all_attachments_flat
    - Returns all entries regardless of type
    - Sorted correctly

  Integration
    - attach + list shows file
    - attach + detach + list shows empty
    - Two project-type attachments with same filename → list shows only latest
    - Attach to non-latest snapshot, list at latest → correct inheritance
"""

import json
import shutil
import struct
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.init import PROJECTDATA_MAGIC, PROJECTDATA_MAGIC_OFFSET
from core.models import (
    Project,
    ProjectPaths,
    Snapshot,
    SidecarEntry,
    SidecarType,
    StorageProvider,
)
from core.sidecar import (
    AttachResult,
    DetachResult,
    ListAttachmentsResult,
    SidecarItem,
    all_attachments_flat,
    do_attach,
    do_detach,
    list_attachments,
    resolve_attachments_at,
)


# ─────────────────────────────────────────────────────────────
# SHARED FIXTURES / HELPERS
# ─────────────────────────────────────────────────────────────

def make_provider(tmp: Path) -> StorageProvider:
    return StorageProvider.local(tmp / "BandTracker")


def make_band(tmp: Path, name: str = "TestProject") -> Path:
    """Minimal valid .band bundle."""
    band = tmp / f"{name}.band"
    (band / "Alternatives" / "000").mkdir(parents=True)
    (band / "Media" / "Audio Files").mkdir(parents=True)
    data = bytearray(512)
    data[PROJECTDATA_MAGIC_OFFSET:PROJECTDATA_MAGIC_OFFSET + 4] = PROJECTDATA_MAGIC
    (band / "Alternatives" / "000" / "ProjectData").write_bytes(data)
    return band


def setup_project(
    tmp: Path,
    name: str = "TestProject",
    num_snapshots: int = 1,
) -> tuple[StorageProvider, str, ProjectPaths]:
    """
    Build a minimal project structure directly without calling initialize().
    Returns (provider, project_name, paths).

    Creates num_snapshots snapshots with valid meta.json and sidecar/ dirs.
    project.json has latest_snapshot=num_snapshots and next_snapshot_index=num_snapshots+1.
    """
    from datetime import datetime, timezone
    from core.init import write_json_atomic
    from core.models import Handoff, ManifestEntry

    provider = make_provider(tmp)
    project_root = provider.project_path(name)
    paths = ProjectPaths(project_root)

    # Create directories
    for d in [paths.live, paths.media, paths.snapshots, paths.docs]:
        d.mkdir(parents=True, exist_ok=True)

    # Create live bundle
    band = paths.live_band(name)
    (band / "Alternatives" / "000").mkdir(parents=True)
    (band / "Media" / "Audio Files").mkdir(parents=True)
    data = bytearray(512)
    data[PROJECTDATA_MAGIC_OFFSET:PROJECTDATA_MAGIC_OFFSET + 4] = PROJECTDATA_MAGIC
    pd_path = band / "Alternatives" / "000" / "ProjectData"
    pd_path.write_bytes(data)

    # Create snapshots
    for i in range(1, num_snapshots + 1):
        snap_dir = paths.snapshot(i)
        snap_dir.mkdir(parents=True, exist_ok=True)
        paths.snapshot_sidecar(i).mkdir(parents=True, exist_ok=True)

        import shutil as _shutil
        _shutil.copy2(pd_path, paths.snapshot_project_data(i))

        snap = Snapshot(
            index=i,
            description=f"Snapshot {i}",
            timestamp=datetime.now(timezone.utc),
            author="test@test.com",
            sidecar_files=[],
        )
        write_json_atomic(paths.snapshot_meta(i), snap.to_json())
        write_json_atomic(
            paths.snapshot_manifest(i),
            json.dumps([], indent=2),
        )

    # project.json
    from core.models import Collaborator
    project = Project(
        name=name,
        uuid="test-uuid-1234",
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        owner="test@test.com",
        collaborators=[Collaborator("Tester", "test@test.com")],
        latest_snapshot=num_snapshots,
        next_snapshot_index=num_snapshots + 1,
    )
    write_json_atomic(paths.project_json, project.to_json())
    write_json_atomic(paths.handoff_json, Handoff.open().to_json())

    return provider, name, paths


def make_file(tmp: Path, filename: str, content: bytes = b"test content") -> Path:
    """Create a temporary file with the given content."""
    tmp.mkdir(parents=True, exist_ok=True)
    f = tmp / filename
    f.write_bytes(content)
    return f


# ─────────────────────────────────────────────────────────────
# SIDECARENTRY / SIDECARTYPE
# ─────────────────────────────────────────────────────────────

class TestSidecarEntry:
    def test_to_dict_version(self):
        e = SidecarEntry(filename="bounce.m4a", type=SidecarType.VERSION)
        d = e.to_dict()
        assert d == {"filename": "bounce.m4a", "type": "version"}

    def test_to_dict_project(self):
        e = SidecarEntry(filename="lyrics.txt", type=SidecarType.PROJECT)
        d = e.to_dict()
        assert d == {"filename": "lyrics.txt", "type": "project"}

    def test_from_dict_version(self):
        e = SidecarEntry.from_dict({"filename": "notes.md", "type": "version"})
        assert e.filename == "notes.md"
        assert e.type == SidecarType.VERSION

    def test_from_dict_project(self):
        e = SidecarEntry.from_dict({"filename": "chords.txt", "type": "project"})
        assert e.type == SidecarType.PROJECT

    def test_from_legacy_plain_string(self):
        e = SidecarEntry.from_legacy("oldfile.txt")
        assert e.filename == "oldfile.txt"
        assert e.type == SidecarType.VERSION

    def test_round_trip(self):
        original = SidecarEntry(filename="test.pdf", type=SidecarType.PROJECT)
        restored = SidecarEntry.from_dict(original.to_dict())
        assert restored == original


# ─────────────────────────────────────────────────────────────
# SNAPSHOT MODEL BACKWARD-COMPAT
# ─────────────────────────────────────────────────────────────

class TestSnapshotBackwardCompat:
    def test_old_string_list_reads_as_version(self):
        """Pre-Increment-8: sidecar_files was list[str]."""
        from datetime import datetime, timezone
        snap = Snapshot(
            index=1,
            description="test",
            timestamp=datetime.now(timezone.utc),
            author="a@b.com",
        )
        raw = snap.to_dict()
        # Simulate old format: plain strings
        raw["sidecar_files"] = ["bounce.m4a", "notes.txt"]
        restored = Snapshot.from_dict(raw)
        assert len(restored.sidecar_files) == 2
        assert all(isinstance(e, SidecarEntry) for e in restored.sidecar_files)
        assert restored.sidecar_files[0].filename == "bounce.m4a"
        assert restored.sidecar_files[0].type == SidecarType.VERSION
        assert restored.sidecar_files[1].type == SidecarType.VERSION

    def test_new_dict_list_reads_correctly(self):
        from datetime import datetime, timezone
        snap = Snapshot(
            index=1,
            description="test",
            timestamp=datetime.now(timezone.utc),
            author="a@b.com",
            sidecar_files=[
                SidecarEntry("bounce.m4a", SidecarType.VERSION),
                SidecarEntry("lyrics.txt", SidecarType.PROJECT),
            ],
        )
        restored = Snapshot.from_json(snap.to_json())
        assert len(restored.sidecar_files) == 2
        assert restored.sidecar_files[0].type == SidecarType.VERSION
        assert restored.sidecar_files[1].type == SidecarType.PROJECT

    def test_empty_sidecar_files(self):
        from datetime import datetime, timezone
        snap = Snapshot(
            index=1,
            description="test",
            timestamp=datetime.now(timezone.utc),
            author="a@b.com",
        )
        raw = snap.to_dict()
        assert raw["sidecar_files"] == []
        restored = Snapshot.from_dict(raw)
        assert restored.sidecar_files == []

    def test_mixed_old_and_new_entries(self):
        """A JSON with mixed strings and dicts is handled gracefully."""
        from datetime import datetime, timezone
        snap = Snapshot(
            index=1,
            description="test",
            timestamp=datetime.now(timezone.utc),
            author="a@b.com",
        )
        raw = snap.to_dict()
        # Simulate a hypothetical mixed state
        raw["sidecar_files"] = [
            "oldstyle.txt",
            {"filename": "newstyle.md", "type": "project"},
        ]
        restored = Snapshot.from_dict(raw)
        assert restored.sidecar_files[0].filename == "oldstyle.txt"
        assert restored.sidecar_files[0].type == SidecarType.VERSION
        assert restored.sidecar_files[1].filename == "newstyle.md"
        assert restored.sidecar_files[1].type == SidecarType.PROJECT


# ─────────────────────────────────────────────────────────────
# do_attach
# ─────────────────────────────────────────────────────────────

class TestDoAttach:
    def test_attach_version_happy_path(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=1)
        src = make_file(tmp_path, "bounce.m4a", b"audio data")

        result = do_attach(paths, src, SidecarType.VERSION, snapshot_index=1)

        assert result.ok
        assert result.filename == "bounce.m4a"
        assert result.sidecar_type == SidecarType.VERSION
        assert result.snapshot_index == 1
        assert not result.overwritten

    def test_attach_project_happy_path(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=1)
        src = make_file(tmp_path, "lyrics.txt", b"verse 1")

        result = do_attach(paths, src, SidecarType.PROJECT, snapshot_index=1)

        assert result.ok
        assert result.sidecar_type == SidecarType.PROJECT

    def test_file_copied_to_sidecar_dir(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=1)
        src = make_file(tmp_path, "notes.md", b"my notes")

        do_attach(paths, src, SidecarType.VERSION, snapshot_index=1)

        dest = paths.snapshot_sidecar_file(1, "notes.md")
        assert dest.exists()
        assert dest.read_bytes() == b"my notes"

    def test_file_content_matches_source(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=1)
        content = b"\x00\xff\xab" * 100
        src = make_file(tmp_path, "data.bin", content)

        do_attach(paths, src, SidecarType.VERSION, snapshot_index=1)

        assert paths.snapshot_sidecar_file(1, "data.bin").read_bytes() == content

    def test_meta_json_updated(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=1)
        src = make_file(tmp_path, "chords.txt")

        do_attach(paths, src, SidecarType.PROJECT, snapshot_index=1)

        snap = Snapshot.from_json(paths.snapshot_meta(1).read_text())
        names = [e.filename for e in snap.sidecar_files]
        assert "chords.txt" in names
        entry = next(e for e in snap.sidecar_files if e.filename == "chords.txt")
        assert entry.type == SidecarType.PROJECT

    def test_defaults_to_latest_snapshot(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=3)
        src = make_file(tmp_path, "test.txt")

        result = do_attach(paths, src, SidecarType.VERSION)

        assert result.ok
        assert result.snapshot_index == 3

    def test_explicit_snapshot_index(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=3)
        src = make_file(tmp_path, "old.txt")

        result = do_attach(paths, src, SidecarType.VERSION, snapshot_index=2)

        assert result.ok
        assert result.snapshot_index == 2
        snap = Snapshot.from_json(paths.snapshot_meta(2).read_text())
        assert any(e.filename == "old.txt" for e in snap.sidecar_files)
        # Snapshot 3 should NOT have it
        snap3 = Snapshot.from_json(paths.snapshot_meta(3).read_text())
        assert not any(e.filename == "old.txt" for e in snap3.sidecar_files)

    def test_missing_source_file_returns_error(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=1)
        missing = tmp_path / "ghost.txt"

        result = do_attach(paths, missing, SidecarType.VERSION, snapshot_index=1)

        assert not result.ok
        assert len(result.errors) > 0
        assert "ghost.txt" in result.errors[0]

    def test_source_is_directory_returns_error(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=1)
        d = tmp_path / "a_directory"
        d.mkdir()

        result = do_attach(paths, d, SidecarType.VERSION, snapshot_index=1)

        assert not result.ok
        assert len(result.errors) > 0

    def test_nonexistent_snapshot_returns_error(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=1)
        src = make_file(tmp_path, "test.txt")

        result = do_attach(paths, src, SidecarType.VERSION, snapshot_index=99)

        assert not result.ok
        assert len(result.errors) > 0

    def test_overwrite_same_filename_same_snapshot(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=1)
        src1 = make_file(tmp_path, "notes.txt", b"version 1")
        do_attach(paths, src1, SidecarType.VERSION, snapshot_index=1)

        src2 = make_file(tmp_path, "notes.txt", b"version 2")
        result = do_attach(paths, src2, SidecarType.VERSION, snapshot_index=1)

        assert result.ok
        assert result.overwritten
        assert len(result.warnings) == 1
        assert "overwritten" in result.warnings[0].lower()
        # File should have new content
        dest = paths.snapshot_sidecar_file(1, "notes.txt")
        assert dest.read_bytes() == b"version 2"

    def test_overwrite_does_not_duplicate_meta_entry(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=1)
        src1 = make_file(tmp_path, "dup.txt", b"v1")
        do_attach(paths, src1, SidecarType.VERSION, snapshot_index=1)
        src2 = make_file(tmp_path, "dup.txt", b"v2")
        do_attach(paths, src2, SidecarType.VERSION, snapshot_index=1)

        snap = Snapshot.from_json(paths.snapshot_meta(1).read_text())
        filenames = [e.filename for e in snap.sidecar_files]
        assert filenames.count("dup.txt") == 1

    def test_same_filename_different_type_is_rejected(self, tmp_path):
        """Attaching the same filename with a different type must be an error."""
        _, _, paths = setup_project(tmp_path, num_snapshots=1)
        src = make_file(tmp_path, "notes.txt", b"content")
        do_attach(paths, src, SidecarType.VERSION, snapshot_index=1)

        src2 = make_file(tmp_path, "notes.txt", b"updated content")
        result = do_attach(paths, src2, SidecarType.PROJECT, snapshot_index=1)

        assert not result.ok
        assert len(result.errors) > 0
        assert "notes.txt" in result.errors[0]
        assert "version" in result.errors[0]
        assert "project" in result.errors[0]

    def test_same_filename_different_type_does_not_overwrite_file(self, tmp_path):
        """Rejected type-conflict must leave the original file intact."""
        _, _, paths = setup_project(tmp_path, num_snapshots=1)
        src = make_file(tmp_path, "notes.txt", b"original")
        do_attach(paths, src, SidecarType.VERSION, snapshot_index=1)

        src2 = make_file(tmp_path, "notes.txt", b"should not overwrite")
        do_attach(paths, src2, SidecarType.PROJECT, snapshot_index=1)

        assert paths.snapshot_sidecar_file(1, "notes.txt").read_bytes() == b"original"

    def test_same_filename_same_type_overwrite_preserves_type(self, tmp_path):
        """Overwriting with same type must not change the type in meta.json."""
        _, _, paths = setup_project(tmp_path, num_snapshots=1)
        src1 = make_file(tmp_path, "notes.txt", b"v1")
        do_attach(paths, src1, SidecarType.PROJECT, snapshot_index=1)

        src2 = make_file(tmp_path, "notes.txt", b"v2")
        do_attach(paths, src2, SidecarType.PROJECT, snapshot_index=1)

        snap = Snapshot.from_json(paths.snapshot_meta(1).read_text())
        entry = next(e for e in snap.sidecar_files if e.filename == "notes.txt")
        assert entry.type == SidecarType.PROJECT

    def test_different_filenames_both_present(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=1)
        do_attach(paths, make_file(tmp_path, "a.txt"), SidecarType.VERSION, snapshot_index=1)
        do_attach(paths, make_file(tmp_path, "b.txt"), SidecarType.PROJECT, snapshot_index=1)

        snap = Snapshot.from_json(paths.snapshot_meta(1).read_text())
        names = {e.filename for e in snap.sidecar_files}
        assert {"a.txt", "b.txt"} == names

    def test_sidecar_dir_created_if_missing(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=1)
        # Remove sidecar dir
        sidecar_dir = paths.snapshot_sidecar(1)
        if sidecar_dir.exists():
            shutil.rmtree(sidecar_dir)

        src = make_file(tmp_path, "test.txt")
        result = do_attach(paths, src, SidecarType.VERSION, snapshot_index=1)

        assert result.ok
        assert sidecar_dir.exists()


# ─────────────────────────────────────────────────────────────
# do_detach
# ─────────────────────────────────────────────────────────────

class TestDoDetach:
    def _attach(self, paths, tmp_path, filename, stype, index=1, content=b"data"):
        src = make_file(tmp_path, filename, content)
        result = do_attach(paths, src, stype, snapshot_index=index)
        assert result.ok

    def test_detach_removes_file_from_disk(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=1)
        self._attach(paths, tmp_path, "notes.txt", SidecarType.VERSION)

        result = do_detach(paths, "notes.txt", snapshot_index=1)

        assert result.ok
        assert not paths.snapshot_sidecar_file(1, "notes.txt").exists()

    def test_detach_removes_entry_from_meta(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=1)
        self._attach(paths, tmp_path, "notes.txt", SidecarType.VERSION)

        do_detach(paths, "notes.txt", snapshot_index=1)

        snap = Snapshot.from_json(paths.snapshot_meta(1).read_text())
        assert not any(e.filename == "notes.txt" for e in snap.sidecar_files)

    def test_detach_defaults_to_latest(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=3)
        self._attach(paths, tmp_path, "test.txt", SidecarType.VERSION, index=3)

        result = do_detach(paths, "test.txt")

        assert result.ok
        assert result.snapshot_index == 3

    def test_detach_explicit_snapshot(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=3)
        self._attach(paths, tmp_path, "test.txt", SidecarType.VERSION, index=2)

        result = do_detach(paths, "test.txt", snapshot_index=2)

        assert result.ok
        assert result.snapshot_index == 2

    def test_detach_filename_not_attached_returns_error(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=1)

        result = do_detach(paths, "nonexistent.txt", snapshot_index=1)

        assert not result.ok
        assert len(result.errors) > 0
        assert "nonexistent.txt" in result.errors[0]

    def test_detach_nonexistent_snapshot_returns_error(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=1)

        result = do_detach(paths, "test.txt", snapshot_index=99)

        assert not result.ok

    def test_detach_missing_file_on_disk_warns_updates_meta(self, tmp_path):
        """File deleted externally → detach should warn but still update meta.json."""
        _, _, paths = setup_project(tmp_path, num_snapshots=1)
        self._attach(paths, tmp_path, "gone.txt", SidecarType.VERSION)
        # Delete the file manually, bypassing detach
        paths.snapshot_sidecar_file(1, "gone.txt").unlink()

        result = do_detach(paths, "gone.txt", snapshot_index=1)

        assert result.ok
        assert len(result.warnings) > 0
        snap = Snapshot.from_json(paths.snapshot_meta(1).read_text())
        assert not any(e.filename == "gone.txt" for e in snap.sidecar_files)

    def test_detach_only_removes_from_specified_snapshot(self, tmp_path):
        """Detach from snapshot 2 should leave snapshot 1's entry intact."""
        _, _, paths = setup_project(tmp_path, num_snapshots=2)
        # Attach same filename to both snapshots
        src1 = make_file(tmp_path / "s1", "lyrics.txt", b"verse 1")
        src2 = make_file(tmp_path / "s2", "lyrics.txt", b"verse 2")
        do_attach(paths, src1, SidecarType.PROJECT, snapshot_index=1)
        do_attach(paths, src2, SidecarType.PROJECT, snapshot_index=2)

        do_detach(paths, "lyrics.txt", snapshot_index=2)

        # Snapshot 1 still has it
        snap1 = Snapshot.from_json(paths.snapshot_meta(1).read_text())
        assert any(e.filename == "lyrics.txt" for e in snap1.sidecar_files)

    def test_detach_returns_correct_filename(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=1)
        self._attach(paths, tmp_path, "file.txt", SidecarType.VERSION)

        result = do_detach(paths, "file.txt", snapshot_index=1)

        assert result.filename == "file.txt"


# ─────────────────────────────────────────────────────────────
# list_attachments — resolved mode
# ─────────────────────────────────────────────────────────────

class TestListAttachmentsResolved:
    def test_empty_project_returns_empty_list(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=1)

        result = list_attachments(paths, snapshot_index=1)

        assert result.ok
        assert result.items == []

    def test_version_entry_shown_at_own_snapshot(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=1)
        do_attach(paths, make_file(tmp_path, "v.txt"), SidecarType.VERSION, snapshot_index=1)

        result = list_attachments(paths, snapshot_index=1)

        assert result.ok
        assert len(result.items) == 1
        assert result.items[0].filename == "v.txt"
        assert result.items[0].sidecar_type == SidecarType.VERSION

    def test_version_entry_not_visible_at_other_snapshots(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=2)
        do_attach(paths, make_file(tmp_path, "v.txt"), SidecarType.VERSION, snapshot_index=1)

        # At snapshot 2, the version attachment from snapshot 1 should NOT appear
        result = list_attachments(paths, snapshot_index=2)

        assert result.ok
        assert not any(i.filename == "v.txt" for i in result.items)

    def test_project_entry_inherits_forward(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=3)
        do_attach(paths, make_file(tmp_path, "lyrics.txt", b"v1"), SidecarType.PROJECT, snapshot_index=1)

        # Should be visible at snapshots 2 and 3
        result2 = list_attachments(paths, snapshot_index=2)
        result3 = list_attachments(paths, snapshot_index=3)

        assert any(i.filename == "lyrics.txt" for i in result2.items)
        assert any(i.filename == "lyrics.txt" for i in result3.items)

    def test_project_shadowing_later_wins(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=3)
        src1 = make_file(tmp_path / "v1", "lyrics.txt", b"old lyrics")
        src3 = make_file(tmp_path / "v3", "lyrics.txt", b"new lyrics")

        do_attach(paths, src1, SidecarType.PROJECT, snapshot_index=1)
        do_attach(paths, src3, SidecarType.PROJECT, snapshot_index=3)

        result = list_attachments(paths, snapshot_index=3)

        items = [i for i in result.items if i.filename == "lyrics.txt"]
        assert len(items) == 1
        # The winning copy is from snapshot 3
        assert items[0].snapshot_index == 3

    def test_project_shadowing_mid_timeline(self, tmp_path):
        """At snapshot 2, the snapshot-1 copy wins (snapshot-3 not yet visible)."""
        _, _, paths = setup_project(tmp_path, num_snapshots=3)
        src1 = make_file(tmp_path / "v1", "lyrics.txt", b"old")
        src3 = make_file(tmp_path / "v3", "lyrics.txt", b"new")

        do_attach(paths, src1, SidecarType.PROJECT, snapshot_index=1)
        do_attach(paths, src3, SidecarType.PROJECT, snapshot_index=3)

        result = list_attachments(paths, snapshot_index=2)

        items = [i for i in result.items if i.filename == "lyrics.txt"]
        assert len(items) == 1
        assert items[0].snapshot_index == 1

    def test_detach_project_reveals_earlier_version(self, tmp_path):
        """Detaching from snapshot 3 should make snapshot 1's copy visible again."""
        _, _, paths = setup_project(tmp_path, num_snapshots=3)
        src1 = make_file(tmp_path / "v1", "lyrics.txt", b"v1 content")
        src3 = make_file(tmp_path / "v3", "lyrics.txt", b"v3 content")

        do_attach(paths, src1, SidecarType.PROJECT, snapshot_index=1)
        do_attach(paths, src3, SidecarType.PROJECT, snapshot_index=3)
        do_detach(paths, "lyrics.txt", snapshot_index=3)

        result = list_attachments(paths, snapshot_index=3)

        items = [i for i in result.items if i.filename == "lyrics.txt"]
        assert len(items) == 1
        assert items[0].snapshot_index == 1

    def test_defaults_to_latest_snapshot(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=2)
        do_attach(paths, make_file(tmp_path, "f.txt"), SidecarType.VERSION, snapshot_index=2)

        result = list_attachments(paths)

        assert result.ok
        assert result.resolved_at_index == 2
        assert any(i.filename == "f.txt" for i in result.items)

    def test_resolved_at_index_set_correctly(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=3)

        result = list_attachments(paths, snapshot_index=2)

        assert result.resolved_at_index == 2

    def test_nonexistent_snapshot_returns_error(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=1)

        result = list_attachments(paths, snapshot_index=99)

        assert not result.ok

    def test_multiple_filenames_all_returned(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=1)
        do_attach(paths, make_file(tmp_path, "a.txt"), SidecarType.VERSION, snapshot_index=1)
        do_attach(paths, make_file(tmp_path, "b.txt"), SidecarType.PROJECT, snapshot_index=1)
        do_attach(paths, make_file(tmp_path, "c.txt"), SidecarType.VERSION, snapshot_index=1)

        result = list_attachments(paths, snapshot_index=1)

        names = {i.filename for i in result.items}
        assert {"a.txt", "b.txt", "c.txt"} == names

    def test_size_bytes_populated(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=1)
        content = b"x" * 512
        do_attach(paths, make_file(tmp_path, "sized.txt", content), SidecarType.VERSION, snapshot_index=1)

        result = list_attachments(paths, snapshot_index=1)

        item = next(i for i in result.items if i.filename == "sized.txt")
        assert item.size_bytes == 512


# ─────────────────────────────────────────────────────────────
# list_attachments — --all mode
# ─────────────────────────────────────────────────────────────

class TestListAttachmentsAll:
    def test_all_returns_every_attachment(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=3)
        do_attach(paths, make_file(tmp_path, "a.txt"), SidecarType.VERSION, snapshot_index=1)
        do_attach(paths, make_file(tmp_path, "b.txt"), SidecarType.PROJECT, snapshot_index=2)
        do_attach(paths, make_file(tmp_path, "c.txt"), SidecarType.VERSION, snapshot_index=3)
        src2 = make_file(tmp_path / "dup", "a.txt", b"different")
        do_attach(paths, src2, SidecarType.PROJECT, snapshot_index=3)

        result = list_attachments(paths, all_snapshots=True)

        assert result.ok
        assert result.all_snapshots
        assert len(result.items) == 4

    def test_all_sorted_by_snapshot_then_type_then_filename(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=2)
        do_attach(paths, make_file(tmp_path, "z.txt"), SidecarType.VERSION, snapshot_index=1)
        do_attach(paths, make_file(tmp_path, "a.txt"), SidecarType.PROJECT, snapshot_index=1)
        do_attach(paths, make_file(tmp_path, "m.txt"), SidecarType.VERSION, snapshot_index=2)

        result = list_attachments(paths, all_snapshots=True)

        assert result.ok
        indices = [i.snapshot_index for i in result.items]
        assert indices == sorted(indices)

    def test_all_ignores_snapshot_index_arg(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=2)
        do_attach(paths, make_file(tmp_path, "snap1.txt"), SidecarType.VERSION, snapshot_index=1)
        do_attach(paths, make_file(tmp_path, "snap2.txt"), SidecarType.VERSION, snapshot_index=2)

        result = list_attachments(paths, snapshot_index=1, all_snapshots=True)

        assert result.ok
        names = {i.filename for i in result.items}
        assert "snap1.txt" in names
        assert "snap2.txt" in names

    def test_all_empty_project(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=1)

        result = list_attachments(paths, all_snapshots=True)

        assert result.ok
        assert result.items == []

    def test_all_shows_both_versions_of_project_type(self, tmp_path):
        """--all should show every copy of a project-type file, not just the latest."""
        _, _, paths = setup_project(tmp_path, num_snapshots=3)
        src1 = make_file(tmp_path / "v1", "lyrics.txt", b"old")
        src3 = make_file(tmp_path / "v3", "lyrics.txt", b"new")

        do_attach(paths, src1, SidecarType.PROJECT, snapshot_index=1)
        do_attach(paths, src3, SidecarType.PROJECT, snapshot_index=3)

        result = list_attachments(paths, all_snapshots=True)

        lyrics_items = [i for i in result.items if i.filename == "lyrics.txt"]
        assert len(lyrics_items) == 2
        snap_indices = {i.snapshot_index for i in lyrics_items}
        assert snap_indices == {1, 3}


# ─────────────────────────────────────────────────────────────
# resolve_attachments_at (unit)
# ─────────────────────────────────────────────────────────────

class TestResolveAttachmentsAt:
    def test_version_and_project_both_returned_at_own_snapshot(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=1)
        do_attach(paths, make_file(tmp_path, "v.txt"), SidecarType.VERSION, snapshot_index=1)
        do_attach(paths, make_file(tmp_path, "p.txt"), SidecarType.PROJECT, snapshot_index=1)

        items = resolve_attachments_at(paths, 1)

        names = {i.filename for i in items}
        assert {"v.txt", "p.txt"} == names

    def test_project_from_snap3_visible_at_snap5(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=5)
        do_attach(paths, make_file(tmp_path, "p.txt"), SidecarType.PROJECT, snapshot_index=3)

        items = resolve_attachments_at(paths, 5)

        assert any(i.filename == "p.txt" for i in items)

    def test_version_from_snap3_not_visible_at_snap5(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=5)
        do_attach(paths, make_file(tmp_path, "v.txt"), SidecarType.VERSION, snapshot_index=3)

        items = resolve_attachments_at(paths, 5)

        assert not any(i.filename == "v.txt" for i in items)

    def test_project_from_snap7_shadows_snap3_at_snap7(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=7)
        s3 = make_file(tmp_path / "s3", "p.txt", b"v3")
        s7 = make_file(tmp_path / "s7", "p.txt", b"v7")

        do_attach(paths, s3, SidecarType.PROJECT, snapshot_index=3)
        do_attach(paths, s7, SidecarType.PROJECT, snapshot_index=7)

        items = resolve_attachments_at(paths, 7)
        p_items = [i for i in items if i.filename == "p.txt"]
        assert len(p_items) == 1
        assert p_items[0].snapshot_index == 7

    def test_empty_result_for_snapshot_with_no_attachments(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=1)

        items = resolve_attachments_at(paths, 1)

        assert items == []


# ─────────────────────────────────────────────────────────────
# all_attachments_flat (unit)
# ─────────────────────────────────────────────────────────────

class TestAllAttachmentsFlat:
    def test_returns_all_entries(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=2)
        do_attach(paths, make_file(tmp_path, "a.txt"), SidecarType.VERSION, snapshot_index=1)
        do_attach(paths, make_file(tmp_path, "b.txt"), SidecarType.PROJECT, snapshot_index=2)

        items = all_attachments_flat(paths)

        assert len(items) == 2

    def test_sorted_by_snapshot_index(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=3)
        do_attach(paths, make_file(tmp_path, "c.txt"), SidecarType.VERSION, snapshot_index=3)
        do_attach(paths, make_file(tmp_path, "a.txt"), SidecarType.VERSION, snapshot_index=1)

        items = all_attachments_flat(paths)

        indices = [i.snapshot_index for i in items]
        assert indices == sorted(indices)

    def test_empty_project(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=1)

        items = all_attachments_flat(paths)

        assert items == []


# ─────────────────────────────────────────────────────────────
# SidecarItem.to_dict
# ─────────────────────────────────────────────────────────────

class TestSidecarItemToDict:
    def test_to_dict_fields(self):
        item = SidecarItem(
            filename="test.txt",
            sidecar_type=SidecarType.PROJECT,
            snapshot_index=5,
            size_bytes=1024,
        )
        d = item.to_dict()
        assert d["filename"] == "test.txt"
        assert d["type"] == "project"
        assert d["snapshot_index"] == 5
        assert d["size_bytes"] == 1024


# ─────────────────────────────────────────────────────────────
# INTEGRATION: attach → list → detach → list
# ─────────────────────────────────────────────────────────────

class TestIntegration:
    def test_attach_then_list_shows_file(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=1)
        do_attach(paths, make_file(tmp_path, "notes.txt"), SidecarType.VERSION, snapshot_index=1)

        result = list_attachments(paths, snapshot_index=1)

        assert any(i.filename == "notes.txt" for i in result.items)

    def test_attach_detach_list_shows_empty(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=1)
        do_attach(paths, make_file(tmp_path, "notes.txt"), SidecarType.VERSION, snapshot_index=1)
        do_detach(paths, "notes.txt", snapshot_index=1)

        result = list_attachments(paths, snapshot_index=1)

        assert result.items == []

    def test_two_project_attachments_same_filename_list_shows_latest(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=2)
        src1 = make_file(tmp_path / "v1", "lyrics.txt", b"draft")
        src2 = make_file(tmp_path / "v2", "lyrics.txt", b"final")

        do_attach(paths, src1, SidecarType.PROJECT, snapshot_index=1)
        do_attach(paths, src2, SidecarType.PROJECT, snapshot_index=2)

        result = list_attachments(paths, snapshot_index=2)

        items = [i for i in result.items if i.filename == "lyrics.txt"]
        assert len(items) == 1
        assert items[0].snapshot_index == 2

    def test_attach_to_old_snapshot_visible_at_latest_via_inheritance(self, tmp_path):
        _, _, paths = setup_project(tmp_path, num_snapshots=5)
        do_attach(paths, make_file(tmp_path, "chords.txt"), SidecarType.PROJECT, snapshot_index=2)

        result = list_attachments(paths)  # defaults to latest (5)

        assert any(i.filename == "chords.txt" for i in result.items)
        item = next(i for i in result.items if i.filename == "chords.txt")
        assert item.snapshot_index == 2

    def test_full_workflow_multiple_types(self, tmp_path):
        """
        Scenario:
          snap 1: attach bounce.m4a [version], lyrics.txt [project]
          snap 2: attach lyrics.txt [project] (update)
          snap 3: (no attachments)

        At snap 3:
          - bounce.m4a NOT visible (version, pinned to snap 1)
          - lyrics.txt VISIBLE, from snap 2 (shadowing snap 1)
        """
        _, _, paths = setup_project(tmp_path, num_snapshots=3)
        s1_bounce = make_file(tmp_path / "s1", "bounce.m4a", b"audio")
        s1_lyrics = make_file(tmp_path / "s1" / "ly", "lyrics.txt", b"draft")
        s2_lyrics = make_file(tmp_path / "s2", "lyrics.txt", b"final")

        do_attach(paths, s1_bounce, SidecarType.VERSION, snapshot_index=1)
        do_attach(paths, s1_lyrics, SidecarType.PROJECT, snapshot_index=1)
        do_attach(paths, s2_lyrics, SidecarType.PROJECT, snapshot_index=2)

        result = list_attachments(paths, snapshot_index=3)

        names = {i.filename for i in result.items}
        assert "bounce.m4a" not in names
        assert "lyrics.txt" in names
        lyrics = next(i for i in result.items if i.filename == "lyrics.txt")
        assert lyrics.snapshot_index == 2
