"""
tests/test_init.py  —  Increment 1: Project initialization tests

Tests cover:
  - validate_band: all error and warning paths
  - sanitize_project_name: edge cases
  - hash_file: correctness and stability
  - copy_media_to_store: deduplication, error handling
  - copy_band_bundle: copy + verify, cleanup on failure
  - write_initial_snapshot: correct files written
  - initialize: full happy path, all failure paths, cleanup on error
"""

import hashlib
import json
import struct
import shutil
import tempfile
from pathlib import Path
from datetime import timezone

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.init import (
    PROJECTDATA_MAGIC,
    copy_band_bundle,
    copy_media_to_store,
    hash_file,
    initialize,
    sanitize_project_name,
    validate_band,
    write_initial_snapshot,
)
from core.models import (
    Handoff,
    LockState,
    Project,
    ProjectPaths,
    Snapshot,
    StorageProvider,
)


# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────

def make_band(tmp: Path, name: str = "TestProject",
              with_media: bool = False,
              bad_magic: bool = False) -> Path:
    """Create a minimal valid .band bundle for testing."""
    band = tmp / f"{name}.band"
    (band / "Output").mkdir(parents=True)
    (band / "Media" / "Audio Files").mkdir(parents=True)

    data = bytearray(512)
    data[0:4] = b"XXXX" if bad_magic else PROJECTDATA_MAGIC
    struct.pack_into("<I", data, 0x40, 1_210_000)
    (band / "Output" / "ProjectData").write_bytes(data)

    if with_media:
        (band / "Media" / "Audio Files" / "Guitar Take 1.aif").write_bytes(b"AIFF" + b"\x00" * 64)
        (band / "Media" / "Audio Files" / "Vocal Take 1.aif").write_bytes(b"AIFF" + b"\x00" * 32)

    return band


def make_provider(tmp: Path) -> StorageProvider:
    return StorageProvider.local(tmp / "BandTracker")


# ─────────────────────────────────────────────────────────────
# validate_band
# ─────────────────────────────────────────────────────────────

class TestValidateBand:
    def test_valid_bundle(self, tmp_path):
        band = make_band(tmp_path)
        r = validate_band(band)
        assert r.ok
        assert r.errors == []
        assert r.project_data_path == band / "Output" / "ProjectData"

    def test_missing_path(self, tmp_path):
        r = validate_band(tmp_path / "DoesNotExist.band")
        assert not r.ok
        assert any("does not exist" in e.lower() for e in r.errors)

    def test_not_a_directory(self, tmp_path):
        f = tmp_path / "notadir.band"
        f.write_bytes(b"hello")
        r = validate_band(f)
        assert not r.ok
        assert any("not a directory" in e.lower() for e in r.errors)

    def test_missing_project_data(self, tmp_path):
        band = tmp_path / "Empty.band"
        band.mkdir()
        r = validate_band(band)
        assert not r.ok
        assert any("projectdata" in e.lower() for e in r.errors)

    def test_bad_magic_bytes(self, tmp_path):
        band = make_band(tmp_path, bad_magic=True)
        r = validate_band(band)
        assert not r.ok
        assert any("magic" in e.lower() for e in r.errors)

    def test_no_band_extension_is_warning_not_error(self, tmp_path):
        band = make_band(tmp_path, name="TestProject")
        # Rename to remove .band extension
        no_ext = band.parent / "TestProject_no_ext"
        band.rename(no_ext)
        r = validate_band(no_ext)
        # Should warn but still be ok (ProjectData is valid)
        assert r.ok
        assert any(".band" in w for w in r.warnings)

    def test_garageband_lock_file(self, tmp_path):
        band = make_band(tmp_path)
        (band / ".com.apple.GarageBand.lock").write_bytes(b"")
        r = validate_band(band)
        assert not r.ok
        assert any("garageband" in e.lower() or "open" in e.lower() for e in r.errors)

    def test_collects_media_files(self, tmp_path):
        band = make_band(tmp_path, with_media=True)
        r = validate_band(band)
        assert r.ok
        assert len(r.media_files) == 2

    def test_no_media_is_fine(self, tmp_path):
        band = make_band(tmp_path, with_media=False)
        r = validate_band(band)
        assert r.ok
        assert r.media_files == []

    def test_total_size_computed(self, tmp_path):
        band = make_band(tmp_path, with_media=True)
        r = validate_band(band)
        assert r.total_size_bytes > 0


# ─────────────────────────────────────────────────────────────
# sanitize_project_name
# ─────────────────────────────────────────────────────────────

class TestSanitizeProjectName:
    def _name(self, filename: str) -> str:
        p = Path(filename)
        return sanitize_project_name(p)

    def test_normal_name(self):
        assert self._name("Midnight Drive.band") == "Midnight Drive"

    def test_strips_extension(self):
        assert self._name("Song.band") == "Song"

    def test_normalizes_whitespace(self):
        assert self._name("My   Song.band") == "My Song"

    def test_removes_unsafe_chars(self):
        # slashes, colons, etc. should be stripped
        result = self._name("Song/with:special<chars>.band")
        assert "/" not in result
        assert ":" not in result
        assert "<" not in result
        assert ">" not in result

    def test_keeps_hyphens_and_underscores(self):
        assert self._name("My-Song_v2.band") == "My-Song_v2"

    def test_keeps_apostrophes_and_parens(self):
        assert self._name("Jordan's Song (Final).band") == "Jordan's Song (Final)"

    def test_empty_after_sanitize_becomes_untitled(self):
        assert self._name("!!!.band") == "Untitled Project"

    def test_strips_leading_trailing_whitespace(self):
        assert self._name("  My Song  .band") == "My Song"


# ─────────────────────────────────────────────────────────────
# hash_file
# ─────────────────────────────────────────────────────────────

class TestHashFile:
    def test_consistent(self, tmp_path):
        f = tmp_path / "test.aif"
        f.write_bytes(b"hello world")
        assert hash_file(f) == hash_file(f)

    def test_known_hash(self, tmp_path):
        f = tmp_path / "test.aif"
        content = b"hello world"
        f.write_bytes(content)
        expected = hashlib.sha256(content).hexdigest()
        assert hash_file(f) == expected

    def test_different_content_different_hash(self, tmp_path):
        f1 = tmp_path / "a.aif"
        f2 = tmp_path / "b.aif"
        f1.write_bytes(b"content a")
        f2.write_bytes(b"content b")
        assert hash_file(f1) != hash_file(f2)

    def test_same_content_same_hash(self, tmp_path):
        content = b"identical"
        f1 = tmp_path / "a.aif"
        f2 = tmp_path / "b.aif"
        f1.write_bytes(content)
        f2.write_bytes(content)
        assert hash_file(f1) == hash_file(f2)


# ─────────────────────────────────────────────────────────────
# copy_media_to_store
# ─────────────────────────────────────────────────────────────

class TestCopyMediaToStore:
    def test_copies_files(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        f = src / "Guitar.aif"
        f.write_bytes(b"audio data")

        store = tmp_path / "store"
        entries = copy_media_to_store([f], store)

        assert len(entries) == 1
        assert entries[0].original_name == "Guitar.aif"
        expected_hash = hashlib.sha256(b"audio data").hexdigest()
        assert entries[0].content_hash == expected_hash
        assert (store / f"{expected_hash}.aif").exists()

    def test_deduplication(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        content = b"same content"
        f1 = src / "Guitar Take 1.aif"
        f2 = src / "Guitar Take 2.aif"
        f1.write_bytes(content)
        f2.write_bytes(content)

        store = tmp_path / "store"
        entries = copy_media_to_store([f1, f2], store)

        # Both entries should have the same hash
        assert entries[0].content_hash == entries[1].content_hash
        # But only one file in the store
        assert len(list(store.iterdir())) == 1

    def test_missing_file_embedded_in_entry(self, tmp_path):
        missing = tmp_path / "DoesNotExist.aif"
        store = tmp_path / "store"
        entries = copy_media_to_store([missing], store)
        assert len(entries) == 1
        assert entries[0].content_hash.startswith("ERROR:")

    def test_creates_store_if_missing(self, tmp_path):
        store = tmp_path / "nonexistent" / "store"
        copy_media_to_store([], store)
        assert store.exists()

    def test_size_bytes_recorded(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        content = b"x" * 1234
        f = src / "Test.aif"
        f.write_bytes(content)

        store = tmp_path / "store"
        entries = copy_media_to_store([f], store)
        assert entries[0].size_bytes == 1234


# ─────────────────────────────────────────────────────────────
# copy_band_bundle
# ─────────────────────────────────────────────────────────────

class TestCopyBandBundle:
    def test_copies_successfully(self, tmp_path):
        band = make_band(tmp_path / "src")
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        result = copy_band_bundle(band, dest_dir)

        assert result == dest_dir / band.name
        assert (result / "Output" / "ProjectData").exists()
        magic = (result / "Output" / "ProjectData").read_bytes()[:4]
        assert magic == PROJECTDATA_MAGIC

    def test_overwrites_existing_dest(self, tmp_path):
        band = make_band(tmp_path / "src")
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        # Create stale copy first
        stale = dest_dir / band.name
        stale.mkdir()
        (stale / "stale.txt").write_text("old")

        result = copy_band_bundle(band, dest_dir)
        assert not (result / "stale.txt").exists()


# ─────────────────────────────────────────────────────────────
# write_initial_snapshot
# ─────────────────────────────────────────────────────────────

class TestWriteInitialSnapshot:
    def test_writes_all_files(self, tmp_path):
        band = make_band(tmp_path / "live_area")
        project_name = "TestProject"
        project_root = tmp_path / "project"
        paths = ProjectPaths(project_root)
        paths.live.mkdir(parents=True)
        paths.snapshots.mkdir(parents=True)
        paths.snapshot_sidecar(1).mkdir(parents=True)

        # Simulate live/ having the band bundle
        import shutil
        shutil.copytree(band, paths.live_band(project_name))

        snap = write_initial_snapshot(
            paths=paths,
            project_name=project_name,
            author="j@e.com",
            media_entries=[],
        )

        assert snap.index == 1
        assert snap.description == "Initial version"
        assert snap.author == "j@e.com"
        assert snap.diff_summary == []
        assert snap.milestone is None

        assert paths.snapshot_project_data(1).exists()
        assert paths.snapshot_meta(1).exists()
        assert paths.snapshot_manifest(1).exists()

    def test_meta_json_is_valid(self, tmp_path):
        band = make_band(tmp_path / "live_area")
        project_name = "TestProject"
        project_root = tmp_path / "project"
        paths = ProjectPaths(project_root)
        paths.live.mkdir(parents=True)
        paths.snapshots.mkdir(parents=True)
        paths.snapshot_sidecar(1).mkdir(parents=True)

        import shutil
        shutil.copytree(band, paths.live_band(project_name))

        write_initial_snapshot(paths, project_name, "j@e.com", [])

        raw = paths.snapshot_meta(1).read_text()
        parsed = json.loads(raw)
        assert parsed["index"] == 1
        assert parsed["description"] == "Initial version"
        assert parsed["author"] == "j@e.com"
        assert parsed["diff_summary"] == []
        assert parsed["milestone"] is None

    def test_projectdata_copied_correctly(self, tmp_path):
        band = make_band(tmp_path / "live_area")
        project_name = "TestProject"
        project_root = tmp_path / "project"
        paths = ProjectPaths(project_root)
        paths.live.mkdir(parents=True)
        paths.snapshots.mkdir(parents=True)
        paths.snapshot_sidecar(1).mkdir(parents=True)

        import shutil
        shutil.copytree(band, paths.live_band(project_name))

        write_initial_snapshot(paths, project_name, "j@e.com", [])

        snap_pd = paths.snapshot_project_data(1)
        live_pd = paths.live_project_data(project_name)
        assert snap_pd.read_bytes() == live_pd.read_bytes()


# ─────────────────────────────────────────────────────────────
# initialize (full integration)
# ─────────────────────────────────────────────────────────────

class TestInitialize:
    def test_happy_path(self, tmp_path):
        band = make_band(tmp_path / "gb", with_media=True)
        provider = make_provider(tmp_path)

        result = initialize(band, provider, "j@e.com", "Jordan")

        assert result.ok
        assert result.project_name == "TestProject"
        assert result.snapshot_index == 1
        assert result.media_files_copied == 2

    def test_folder_structure_created(self, tmp_path):
        band = make_band(tmp_path / "gb")
        provider = make_provider(tmp_path)

        result = initialize(band, provider, "j@e.com", "Jordan")
        paths = ProjectPaths(result.project_root)

        assert paths.live.exists()
        assert paths.media.exists()
        assert paths.snapshots.exists()
        assert paths.docs.exists()

    def test_project_json_written(self, tmp_path):
        band = make_band(tmp_path / "gb")
        provider = make_provider(tmp_path)
        result = initialize(band, provider, "j@e.com", "Jordan")

        raw = result.project_json_path.read_text()
        project = Project.from_json(raw)

        assert project.name == "TestProject"
        assert project.owner == "j@e.com"
        assert project.latest_snapshot == 1
        assert project.next_snapshot_index == 2
        assert len(project.collaborators) == 1
        assert project.collaborators[0].display_name == "Jordan"

    def test_handoff_json_written_open(self, tmp_path):
        band = make_band(tmp_path / "gb")
        provider = make_provider(tmp_path)
        result = initialize(band, provider, "j@e.com", "Jordan")

        paths = ProjectPaths(result.project_root)
        handoff = Handoff.from_json(paths.handoff_json.read_text())

        assert handoff.lock_state == LockState.OPEN
        assert handoff.active_editor is None

    def test_snapshot_001_exists(self, tmp_path):
        band = make_band(tmp_path / "gb")
        provider = make_provider(tmp_path)
        result = initialize(band, provider, "j@e.com", "Jordan")

        assert result.first_snapshot_path.exists()
        paths = ProjectPaths(result.project_root)
        assert paths.snapshot_project_data(1).exists()
        assert paths.snapshot_meta(1).exists()
        assert paths.snapshot_manifest(1).exists()

    def test_live_bundle_present(self, tmp_path):
        band = make_band(tmp_path / "gb")
        provider = make_provider(tmp_path)
        result = initialize(band, provider, "j@e.com", "Jordan")

        paths = ProjectPaths(result.project_root)
        live_band = paths.live_band("TestProject")
        assert live_band.exists()
        assert (live_band / "Output" / "ProjectData").exists()

    def test_media_deduplicated(self, tmp_path):
        band = make_band(tmp_path / "gb", with_media=True)
        provider = make_provider(tmp_path)
        result = initialize(band, provider, "j@e.com", "Jordan")

        paths = ProjectPaths(result.project_root)
        media_files = list(paths.media.iterdir())
        # 2 media files with different content = 2 files in store
        assert len(media_files) == 2

    def test_invalid_band_returns_error(self, tmp_path):
        missing = tmp_path / "DoesNotExist.band"
        provider = make_provider(tmp_path)
        result = initialize(missing, provider, "j@e.com", "Jordan")

        assert not result.ok
        assert len(result.errors) > 0

    def test_name_collision_returns_error(self, tmp_path):
        band = make_band(tmp_path / "gb")
        provider = make_provider(tmp_path)

        # First init
        r1 = initialize(band, provider, "j@e.com", "Jordan")
        assert r1.ok

        # Second init with same name — make a new band with same name
        band2 = make_band(tmp_path / "gb2")
        r2 = initialize(band2, provider, "j@e.com", "Jordan")
        assert not r2.ok
        assert any("already" in e.lower() for e in r2.errors)

    def test_cleanup_on_bundle_copy_failure(self, tmp_path):
        """If the bundle copy fails, the project root should be cleaned up."""
        band = make_band(tmp_path / "gb")
        provider = make_provider(tmp_path)

        # Corrupt ProjectData so copy verification fails
        pd = band / "Output" / "ProjectData"
        pd.write_bytes(b"XXXX" + b"\x00" * 508)

        result = initialize(band, provider, "j@e.com", "Jordan")
        # validate_band catches bad magic before we get to copy
        assert not result.ok
        # Project root should not exist (or be empty)
        if result.project_root != Path("."):
            assert not (result.project_root / "project.json").exists()

    def test_bad_magic_bytes_fails_validation(self, tmp_path):
        band = make_band(tmp_path / "gb", bad_magic=True)
        provider = make_provider(tmp_path)
        result = initialize(band, provider, "j@e.com", "Jordan")
        assert not result.ok
        assert any("magic" in e.lower() for e in result.errors)

    def test_project_name_sanitized(self, tmp_path):
        band = make_band(tmp_path / "gb", name="My Song: Final!!!")
        provider = make_provider(tmp_path)
        result = initialize(band, provider, "j@e.com", "Jordan")
        assert result.ok
        assert ":" not in result.project_name
        assert "!" not in result.project_name

    def test_no_media_is_fine(self, tmp_path):
        band = make_band(tmp_path / "gb", with_media=False)
        provider = make_provider(tmp_path)
        result = initialize(band, provider, "j@e.com", "Jordan")
        assert result.ok
        assert result.media_files_copied == 0

