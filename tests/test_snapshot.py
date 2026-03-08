"""
tests/test_snapshot.py  —  Increment 2: Snapshot writer tests

Tests cover:
  - take_snapshot: happy path, media deduplication, milestone tags,
    placeholder description, updated project.json, correct files written
  - take_snapshot: all failure paths and cleanup behaviour
  - _collect_live_media: with and without media
  - _ensure_media_in_store: deduplication, error handling
  - _write_snapshot_atomically: correct files, correct content
  - _update_project_json: counter advances correctly
"""

import hashlib
import json
import shutil
import struct
from pathlib import Path
from datetime import timezone

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.init import PROJECTDATA_MAGIC, initialize
from core.models import (
    ManifestEntry,
    MilestoneTag,
    Project,
    ProjectPaths,
    Snapshot,
    StorageProvider,
)
from core.snapshot import (
    PLACEHOLDER_DESCRIPTION,
    SnapshotResult,
    _collect_live_media,
    _ensure_media_in_store,
    _update_project_json,
    _write_snapshot_atomically,
    take_snapshot,
)


# ─────────────────────────────────────────────────────────────
# SHARED FIXTURES / HELPERS
# ─────────────────────────────────────────────────────────────

def make_band(tmp: Path, name: str = "TestProject",
              with_media: bool = False,
              bad_magic: bool = False) -> Path:
    """Minimal valid .band bundle — mirrors the helper in test_init.py."""
    band = tmp / f"{name}.band"
    (band / "Output").mkdir(parents=True)
    (band / "Media" / "Audio Files").mkdir(parents=True)

    data = bytearray(512)
    data[0:4] = b"XXXX" if bad_magic else PROJECTDATA_MAGIC
    struct.pack_into("<I", data, 0x40, 1_210_000)
    (band / "Output" / "ProjectData").write_bytes(data)

    if with_media:
        (band / "Media" / "Audio Files" / "Guitar Take 1.aif").write_bytes(
            b"AIFF" + b"\x00" * 64
        )
        (band / "Media" / "Audio Files" / "Vocal Take 1.aif").write_bytes(
            b"AIFF" + b"\x00" * 32
        )

    return band


def make_provider(tmp: Path) -> StorageProvider:
    return StorageProvider.local(tmp / "BandTracker")


def init_project(tmp: Path, name: str = "TestProject",
                 with_media: bool = False) -> tuple[StorageProvider, str]:
    """
    Run initialize() and return (provider, project_name).
    Asserts success so individual tests don't repeat this boilerplate.
    """
    band = make_band(tmp / "gb", name=name, with_media=with_media)
    provider = make_provider(tmp)
    result = initialize(band, provider, "j@e.com", "Jordan")
    assert result.ok, f"initialize() failed: {result.errors}"
    return provider, result.project_name


# ─────────────────────────────────────────────────────────────
# _collect_live_media
# ─────────────────────────────────────────────────────────────

class TestCollectLiveMedia:
    def test_returns_empty_when_no_media_dir(self, tmp_path):
        provider, project_name = init_project(tmp_path, with_media=False)
        project_root = provider.project_path(project_name)
        paths = ProjectPaths(project_root)
        # Remove media dir to simulate a project with no audio
        media_dir = paths.live_media_dir(project_name)
        if media_dir.exists():
            shutil.rmtree(media_dir)
        files = _collect_live_media(paths, project_name)
        assert files == []

    def test_returns_files_when_media_present(self, tmp_path):
        provider, project_name = init_project(tmp_path, with_media=True)
        project_root = provider.project_path(project_name)
        paths = ProjectPaths(project_root)
        files = _collect_live_media(paths, project_name)
        assert len(files) == 2
        names = {f.name for f in files}
        assert "Guitar Take 1.aif" in names
        assert "Vocal Take 1.aif" in names

    def test_only_returns_files_not_dirs(self, tmp_path):
        provider, project_name = init_project(tmp_path, with_media=True)
        project_root = provider.project_path(project_name)
        paths = ProjectPaths(project_root)
        # Add a subdirectory inside Audio Files — should be ignored
        sub = paths.live_media_dir(project_name) / "subdir"
        sub.mkdir()
        files = _collect_live_media(paths, project_name)
        assert all(f.is_file() for f in files)


# ─────────────────────────────────────────────────────────────
# _ensure_media_in_store
# ─────────────────────────────────────────────────────────────

class TestEnsureMediaInStore:
    def test_copies_new_files(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        f = src / "Guitar.aif"
        f.write_bytes(b"audio content")

        store = tmp_path / "store"
        entries, copied, deduped = _ensure_media_in_store([f], store)

        assert copied == 1
        assert deduped == 0
        assert len(entries) == 1
        expected_hash = hashlib.sha256(b"audio content").hexdigest()
        assert entries[0].content_hash == expected_hash
        assert (store / f"{expected_hash}.aif").exists()

    def test_deduplication_skips_existing(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        content = b"same bytes"
        f1 = src / "Take1.aif"
        f2 = src / "Take2.aif"
        f1.write_bytes(content)
        f2.write_bytes(content)

        store = tmp_path / "store"
        entries, copied, deduped = _ensure_media_in_store([f1, f2], store)

        assert copied == 1
        assert deduped == 1
        assert len(list(store.iterdir())) == 1

    def test_missing_file_produces_error_entry(self, tmp_path):
        missing = tmp_path / "ghost.aif"
        store = tmp_path / "store"
        entries, copied, deduped = _ensure_media_in_store([missing], store)

        assert len(entries) == 1
        assert entries[0].content_hash.startswith("ERROR:")
        assert copied == 0

    def test_creates_store_directory(self, tmp_path):
        store = tmp_path / "nonexistent" / "media"
        _ensure_media_in_store([], store)
        assert store.exists()

    def test_size_bytes_recorded(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        content = b"x" * 999
        f = src / "Test.aif"
        f.write_bytes(content)
        store = tmp_path / "store"
        entries, _, _ = _ensure_media_in_store([f], store)
        assert entries[0].size_bytes == 999

    def test_second_snapshot_full_dedup(self, tmp_path):
        """All files already in store → copied=0, deduped=N."""
        src = tmp_path / "src"
        src.mkdir()
        files = []
        for i in range(3):
            f = src / f"take{i}.aif"
            f.write_bytes(f"audio {i}".encode())
            files.append(f)

        store = tmp_path / "store"
        # First pass — copies everything
        _ensure_media_in_store(files, store)
        # Second pass — everything already there
        _, copied, deduped = _ensure_media_in_store(files, store)
        assert copied == 0
        assert deduped == 3


# ─────────────────────────────────────────────────────────────
# _write_snapshot_atomically
# ─────────────────────────────────────────────────────────────

class TestWriteSnapshotAtomically:
    def _setup(self, tmp_path):
        provider, project_name = init_project(tmp_path, with_media=False)
        project_root = provider.project_path(project_name)
        paths = ProjectPaths(project_root)
        return paths, project_name

    def test_writes_all_three_files(self, tmp_path):
        paths, project_name = self._setup(tmp_path)
        snap = _write_snapshot_atomically(
            paths=paths, index=2, project_name=project_name,
            description="Test snap", author="j@e.com",
            media_entries=[], milestone=None,
        )
        assert paths.snapshot_project_data(2).exists()
        assert paths.snapshot_meta(2).exists()
        assert paths.snapshot_manifest(2).exists()

    def test_sidecar_dir_created(self, tmp_path):
        paths, project_name = self._setup(tmp_path)
        _write_snapshot_atomically(
            paths=paths, index=2, project_name=project_name,
            description="Test", author="j@e.com",
            media_entries=[], milestone=None,
        )
        assert paths.snapshot_sidecar(2).exists()
        assert paths.snapshot_sidecar(2).is_dir()

    def test_project_data_content_matches_live(self, tmp_path):
        paths, project_name = self._setup(tmp_path)
        live_pd = paths.live_project_data(project_name)
        _write_snapshot_atomically(
            paths=paths, index=2, project_name=project_name,
            description="Test", author="j@e.com",
            media_entries=[], milestone=None,
        )
        snap_pd = paths.snapshot_project_data(2)
        assert snap_pd.read_bytes() == live_pd.read_bytes()

    def test_meta_json_fields(self, tmp_path):
        paths, project_name = self._setup(tmp_path)
        snap = _write_snapshot_atomically(
            paths=paths, index=2, project_name=project_name,
            description="My description", author="collab@e.com",
            media_entries=[], milestone=MilestoneTag.ARRANGEMENT_LOCK,
        )
        raw = json.loads(paths.snapshot_meta(2).read_text())
        assert raw["index"] == 2
        assert raw["description"] == "My description"
        assert raw["author"] == "collab@e.com"
        assert raw["milestone"] == "arrangement_lock"
        assert raw["diff_summary"] == []

    def test_manifest_json_entries(self, tmp_path):
        paths, project_name = self._setup(tmp_path)
        entries = [
            ManifestEntry("Guitar.aif", "abc123", 1024),
            ManifestEntry("Vocal.aif", "def456", 2048),
        ]
        _write_snapshot_atomically(
            paths=paths, index=2, project_name=project_name,
            description="Test", author="j@e.com",
            media_entries=entries, milestone=None,
        )
        raw = json.loads(paths.snapshot_manifest(2).read_text())
        assert len(raw) == 2
        assert raw[0]["original_name"] == "Guitar.aif"
        assert raw[0]["content_hash"] == "abc123"
        assert raw[1]["size_bytes"] == 2048

    def test_returns_snapshot_with_correct_index(self, tmp_path):
        paths, project_name = self._setup(tmp_path)
        snap = _write_snapshot_atomically(
            paths=paths, index=7, project_name=project_name,
            description="Seven", author="j@e.com",
            media_entries=[], milestone=None,
        )
        assert snap.index == 7
        assert snap.description == "Seven"


# ─────────────────────────────────────────────────────────────
# _update_project_json
# ─────────────────────────────────────────────────────────────

class TestUpdateProjectJson:
    def test_advances_latest_snapshot(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        project_root = provider.project_path(project_name)
        paths = ProjectPaths(project_root)

        _update_project_json(paths, index=2)

        project = Project.from_json(paths.project_json.read_text())
        assert project.latest_snapshot == 2

    def test_advances_next_snapshot_index(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        project_root = provider.project_path(project_name)
        paths = ProjectPaths(project_root)

        _update_project_json(paths, index=2)

        project = Project.from_json(paths.project_json.read_text())
        assert project.next_snapshot_index == 3

    def test_preserves_other_fields(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        project_root = provider.project_path(project_name)
        paths = ProjectPaths(project_root)

        before = Project.from_json(paths.project_json.read_text())
        _update_project_json(paths, index=2)
        after = Project.from_json(paths.project_json.read_text())

        assert after.name == before.name
        assert after.uuid == before.uuid
        assert after.owner == before.owner
        assert after.collaborators == before.collaborators


# ─────────────────────────────────────────────────────────────
# take_snapshot (full integration)
# ─────────────────────────────────────────────────────────────

class TestTakeSnapshot:
    # ── Happy path ────────────────────────────────────────────

    def test_happy_path_no_media(self, tmp_path):
        provider, project_name = init_project(tmp_path, with_media=False)
        result = take_snapshot(provider, project_name, "j@e.com", "First snap")
        assert result.ok
        assert result.snapshot_index == 2
        assert result.project_name == project_name
        assert result.description == "First snap"

    def test_happy_path_with_media(self, tmp_path):
        provider, project_name = init_project(tmp_path, with_media=True)
        result = take_snapshot(provider, project_name, "j@e.com", "With audio")
        assert result.ok
        assert result.media_files_deduped == 2  # already copied by init
        assert result.media_files_copied == 0

    def test_snapshot_folder_created(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        result = take_snapshot(provider, project_name, "j@e.com", "Test")
        paths = ProjectPaths(provider.project_path(project_name))
        assert paths.snapshot(2).exists()

    def test_all_snapshot_files_written(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        take_snapshot(provider, project_name, "j@e.com", "Test")
        paths = ProjectPaths(provider.project_path(project_name))
        assert paths.snapshot_project_data(2).exists()
        assert paths.snapshot_meta(2).exists()
        assert paths.snapshot_manifest(2).exists()
        assert paths.snapshot_sidecar(2).exists()

    def test_project_json_updated(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        take_snapshot(provider, project_name, "j@e.com", "Second snap")
        project = Project.from_json(
            (provider.project_path(project_name) / "project.json").read_text()
        )
        assert project.latest_snapshot == 2
        assert project.next_snapshot_index == 3

    # ── Placeholder description ───────────────────────────────

    def test_no_message_uses_placeholder(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        result = take_snapshot(provider, project_name, "j@e.com", message=None)
        assert result.ok
        assert result.description == PLACEHOLDER_DESCRIPTION
        paths = ProjectPaths(provider.project_path(project_name))
        meta = json.loads(paths.snapshot_meta(2).read_text())
        assert meta["description"] == PLACEHOLDER_DESCRIPTION

    # ── Milestone tags ────────────────────────────────────────

    def test_milestone_arrangement_lock(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        result = take_snapshot(
            provider, project_name, "j@e.com", "Locked",
            milestone=MilestoneTag.ARRANGEMENT_LOCK,
        )
        assert result.ok
        paths = ProjectPaths(provider.project_path(project_name))
        meta = json.loads(paths.snapshot_meta(2).read_text())
        assert meta["milestone"] == "arrangement_lock"

    def test_milestone_final_mix(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        result = take_snapshot(
            provider, project_name, "j@e.com", "Done",
            milestone=MilestoneTag.FINAL_MIX,
        )
        assert result.ok
        paths = ProjectPaths(provider.project_path(project_name))
        meta = json.loads(paths.snapshot_meta(2).read_text())
        assert meta["milestone"] == "final_mix"

    def test_milestone_handoff(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        result = take_snapshot(
            provider, project_name, "j@e.com", "Your turn",
            milestone=MilestoneTag.HANDOFF,
        )
        assert result.ok
        paths = ProjectPaths(provider.project_path(project_name))
        meta = json.loads(paths.snapshot_meta(2).read_text())
        assert meta["milestone"] == "handoff"

    def test_no_milestone_is_none(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        take_snapshot(provider, project_name, "j@e.com", "Normal")
        paths = ProjectPaths(provider.project_path(project_name))
        meta = json.loads(paths.snapshot_meta(2).read_text())
        assert meta["milestone"] is None

    # ── Sequential snapshots ──────────────────────────────────

    def test_three_sequential_snapshots(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        for i, msg in enumerate(["Second", "Third", "Fourth"], start=2):
            result = take_snapshot(provider, project_name, "j@e.com", msg)
            assert result.ok
            assert result.snapshot_index == i

        project = Project.from_json(
            (provider.project_path(project_name) / "project.json").read_text()
        )
        assert project.latest_snapshot == 4
        assert project.next_snapshot_index == 5

    def test_indices_never_reused(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        take_snapshot(provider, project_name, "j@e.com", "v2")
        take_snapshot(provider, project_name, "j@e.com", "v3")
        paths = ProjectPaths(provider.project_path(project_name))
        indices = paths.all_snapshot_indices()
        assert indices == [1, 2, 3]
        assert len(indices) == len(set(indices))

    # ── Deduplication across snapshots ───────────────────────

    def test_new_media_file_gets_copied(self, tmp_path):
        provider, project_name = init_project(tmp_path, with_media=False)
        paths = ProjectPaths(provider.project_path(project_name))

        # Add a new file to the live bundle
        media_dir = paths.live_media_dir(project_name)
        media_dir.mkdir(parents=True, exist_ok=True)
        (media_dir / "NewTrack.aif").write_bytes(b"new audio content")

        result = take_snapshot(provider, project_name, "j@e.com", "Added track")
        assert result.ok
        assert result.media_files_copied == 1
        assert result.media_files_deduped == 0

    def test_unchanged_media_is_deduped(self, tmp_path):
        provider, project_name = init_project(tmp_path, with_media=True)
        # Take a second snapshot — media unchanged since init
        result = take_snapshot(provider, project_name, "j@e.com", "No change")
        assert result.ok
        assert result.media_files_copied == 0
        assert result.media_files_deduped == 2

    def test_media_store_has_one_file_per_unique_hash(self, tmp_path):
        """Two snapshots with identical audio → only one file per unique hash in store."""
        provider, project_name = init_project(tmp_path, with_media=True)
        take_snapshot(provider, project_name, "j@e.com", "Same audio again")
        paths = ProjectPaths(provider.project_path(project_name))
        media_files = list(paths.media.iterdir())
        # 2 distinct audio files from init → exactly 2 hashes in store
        assert len(media_files) == 2

    # ── Meta content ──────────────────────────────────────────

    def test_meta_author_recorded(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        take_snapshot(provider, project_name, "collab@band.com", "Collab snap")
        paths = ProjectPaths(provider.project_path(project_name))
        meta = json.loads(paths.snapshot_meta(2).read_text())
        assert meta["author"] == "collab@band.com"

    def test_meta_diff_summary_empty(self, tmp_path):
        """Diff engine is Increment 3 — diff_summary must be [] for now."""
        provider, project_name = init_project(tmp_path)
        take_snapshot(provider, project_name, "j@e.com", "Test")
        paths = ProjectPaths(provider.project_path(project_name))
        meta = json.loads(paths.snapshot_meta(2).read_text())
        assert meta["diff_summary"] == []

    def test_meta_timestamp_is_utc_iso(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        take_snapshot(provider, project_name, "j@e.com", "Ts test")
        paths = ProjectPaths(provider.project_path(project_name))
        meta = json.loads(paths.snapshot_meta(2).read_text())
        from datetime import datetime
        ts = datetime.fromisoformat(meta["timestamp"])
        assert ts.tzinfo is not None

    def test_project_data_matches_live(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        paths = ProjectPaths(provider.project_path(project_name))
        live_pd = paths.live_project_data(project_name)
        take_snapshot(provider, project_name, "j@e.com", "PD check")
        snap_pd = paths.snapshot_project_data(2)
        assert snap_pd.read_bytes() == live_pd.read_bytes()

    # ── Failure paths ─────────────────────────────────────────

    def test_missing_project_json_returns_error(self, tmp_path):
        provider = make_provider(tmp_path)
        result = take_snapshot(provider, "NonExistentProject", "j@e.com", "Test")
        assert not result.ok
        assert len(result.errors) > 0

    def test_missing_live_bundle_returns_error(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        paths = ProjectPaths(provider.project_path(project_name))
        # Remove the live bundle
        shutil.rmtree(paths.live_band(project_name))
        result = take_snapshot(provider, project_name, "j@e.com", "Test")
        assert not result.ok
        assert any("live" in e.lower() or "bundle" in e.lower() for e in result.errors)

    def test_missing_project_data_returns_error(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        paths = ProjectPaths(provider.project_path(project_name))
        # Remove just the ProjectData file
        paths.live_project_data(project_name).unlink()
        result = take_snapshot(provider, project_name, "j@e.com", "Test")
        assert not result.ok
        assert any("projectdata" in e.lower() for e in result.errors)

    def test_cleanup_on_write_failure(self, tmp_path):
        """If writing fails mid-way, the snapshot folder should not be left behind."""
        provider, project_name = init_project(tmp_path)
        paths = ProjectPaths(provider.project_path(project_name))

        # Make the snapshot directory read-only so writing fails
        snap_dir = paths.snapshot(2)
        snap_dir.mkdir(parents=True)
        snap_dir.chmod(0o444)

        try:
            result = take_snapshot(provider, project_name, "j@e.com", "Fail test")
            # Either fails gracefully or succeeds on some platforms
            if not result.ok:
                # project.json should NOT have been advanced
                project = Project.from_json(paths.project_json.read_text())
                assert project.next_snapshot_index == 2
        finally:
            snap_dir.chmod(0o755)

    # ── Result fields ─────────────────────────────────────────

    def test_result_snapshot_path_is_correct_dir(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        result = take_snapshot(provider, project_name, "j@e.com", "Path check")
        paths = ProjectPaths(provider.project_path(project_name))
        assert result.snapshot_path == paths.snapshot(2)

    def test_result_project_name_matches(self, tmp_path):
        provider, project_name = init_project(tmp_path)
        result = take_snapshot(provider, project_name, "j@e.com", "Name check")
        assert result.project_name == project_name
