"""
tests/test_models.py

Tests for core/models.py.

Coverage:
  - Project: gb_bundle_path and gb_bundle_alias fields
  - Project.create: new factory kwargs
  - Project.to_dict / from_dict: new fields round-trip
  - Project.from_dict: backward compatibility (old JSON without new fields)
  - Snapshot, Handoff, ManifestEntry: existing serialization smoke tests
  - StorageProvider: detect, local factory, path helpers
  - ProjectPaths: all path properties
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.models import (
    Collaborator,
    Handoff,
    LockState,
    ManifestEntry,
    MilestoneTag,
    Project,
    ProjectPaths,
    Snapshot,
    StorageProvider,
    StorageProviderType,
)


# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────

def make_project(**kwargs) -> Project:
    defaults = dict(
        name="Midnight Drive",
        owner_identifier="j@e.com",
        owner_display_name="Jordan",
    )
    defaults.update(kwargs)
    return Project.create(**defaults)


# ─────────────────────────────────────────────────────────────
# Project — gb_bundle fields
# ─────────────────────────────────────────────────────────────

class TestProjectGbBundleFields:
    def test_defaults_to_none(self):
        p = make_project()
        assert p.gb_bundle_path is None
        assert p.gb_bundle_alias is None

    def test_create_accepts_gb_bundle_path(self):
        p = make_project(gb_bundle_path="~/Music/GarageBand/Test.band")
        assert p.gb_bundle_path == "~/Music/GarageBand/Test.band"

    def test_create_accepts_gb_bundle_alias(self):
        p = make_project(gb_bundle_alias="dGVzdA==")
        assert p.gb_bundle_alias == "dGVzdA=="

    def test_to_dict_includes_gb_fields(self):
        p = make_project(
            gb_bundle_path="~/Music/Test.band",
            gb_bundle_alias="abc123",
        )
        d = p.to_dict()
        assert d["gb_bundle_path"] == "~/Music/Test.band"
        assert d["gb_bundle_alias"] == "abc123"

    def test_to_dict_gb_fields_none_when_unset(self):
        p = make_project()
        d = p.to_dict()
        assert d["gb_bundle_path"] is None
        assert d["gb_bundle_alias"] is None

    def test_round_trip_with_gb_fields(self):
        p = make_project(
            gb_bundle_path="~/Music/GarageBand/Song.band",
            gb_bundle_alias="YWJjMTIz",
        )
        p2 = Project.from_json(p.to_json())
        assert p2.gb_bundle_path == p.gb_bundle_path
        assert p2.gb_bundle_alias == p.gb_bundle_alias

    def test_round_trip_without_gb_fields(self):
        p = make_project()
        p2 = Project.from_json(p.to_json())
        assert p2.gb_bundle_path is None
        assert p2.gb_bundle_alias is None

    def test_gb_fields_settable_after_creation(self):
        p = make_project()
        p.gb_bundle_path = "~/Music/Test.band"
        p.gb_bundle_alias = "xyz"
        assert p.gb_bundle_path == "~/Music/Test.band"
        assert p.gb_bundle_alias == "xyz"


# ─────────────────────────────────────────────────────────────
# Project — backward compatibility
# ─────────────────────────────────────────────────────────────

class TestProjectBackwardCompatibility:
    """
    Old project.json files (pre-Increment-5) will not have
    gb_bundle_path or gb_bundle_alias keys. from_dict must handle this.
    """

    def _old_project_dict(self) -> dict:
        """Simulate a project.json written before Increment 5."""
        return {
            "name": "Old Song",
            "uuid": "abc-123",
            "created_at": "2024-01-01T00:00:00+00:00",
            "owner": "j@e.com",
            "collaborators": [
                {"display_name": "Jordan", "identifier": "j@e.com"}
            ],
            "garageband_version": None,
            "latest_snapshot": 3,
            "next_snapshot_index": 4,
            # No gb_bundle_path, no gb_bundle_alias
        }

    def test_from_dict_handles_missing_gb_fields(self):
        d = self._old_project_dict()
        p = Project.from_dict(d)
        assert p.gb_bundle_path is None
        assert p.gb_bundle_alias is None

    def test_from_json_handles_missing_gb_fields(self):
        d = self._old_project_dict()
        p = Project.from_json(json.dumps(d))
        assert p.name == "Old Song"
        assert p.latest_snapshot == 3
        assert p.gb_bundle_path is None

    def test_other_fields_unaffected(self):
        d = self._old_project_dict()
        p = Project.from_dict(d)
        assert p.name == "Old Song"
        assert p.owner == "j@e.com"
        assert p.next_snapshot_index == 4
        assert len(p.collaborators) == 1

    def test_round_trip_after_migration(self):
        """Simulate set-gb writing new fields into an old project."""
        d = self._old_project_dict()
        p = Project.from_dict(d)

        # Simulate set-gb
        p.gb_bundle_path = "~/Music/GarageBand/Old Song.band"
        p.gb_bundle_alias = None

        p2 = Project.from_json(p.to_json())
        assert p2.gb_bundle_path == "~/Music/GarageBand/Old Song.band"
        assert p2.gb_bundle_alias is None
        assert p2.name == "Old Song"


# ─────────────────────────────────────────────────────────────
# Project — core fields (smoke tests)
# ─────────────────────────────────────────────────────────────

class TestProjectCore:
    def test_create_sets_fields(self):
        p = make_project()
        assert p.name == "Midnight Drive"
        assert p.owner == "j@e.com"
        assert len(p.collaborators) == 1
        assert p.collaborators[0].display_name == "Jordan"
        assert p.latest_snapshot is None
        assert p.next_snapshot_index == 1

    def test_uuid_unique_per_instance(self):
        p1 = make_project()
        p2 = make_project()
        assert p1.uuid != p2.uuid

    def test_add_collaborator(self):
        p = make_project()
        c = Collaborator(display_name="Alex", identifier="a@e.com")
        p.add_collaborator(c)
        assert len(p.collaborators) == 2

    def test_add_collaborator_no_duplicate(self):
        p = make_project()
        c = Collaborator(display_name="Jordan", identifier="j@e.com")
        p.add_collaborator(c)
        assert len(p.collaborators) == 1

    def test_get_collaborator_found(self):
        p = make_project()
        c = p.get_collaborator("j@e.com")
        assert c is not None
        assert c.display_name == "Jordan"

    def test_get_collaborator_not_found(self):
        p = make_project()
        assert p.get_collaborator("nobody@e.com") is None

    def test_to_json_from_json_round_trip(self):
        p = make_project()
        p.latest_snapshot = 5
        p.next_snapshot_index = 6
        p2 = Project.from_json(p.to_json())
        assert p2.name == p.name
        assert p2.uuid == p.uuid
        assert p2.latest_snapshot == 5
        assert p2.next_snapshot_index == 6


# ─────────────────────────────────────────────────────────────
# Snapshot serialization
# ─────────────────────────────────────────────────────────────

class TestSnapshotSerialization:
    def _make(self, **kwargs) -> Snapshot:
        defaults = dict(
            index=1,
            description="Initial version",
            timestamp=datetime.now(timezone.utc),
            author="j@e.com",
        )
        defaults.update(kwargs)
        return Snapshot(**defaults)

    def test_round_trip(self):
        s = self._make(index=3, description="Added bridge")
        s2 = Snapshot.from_json(s.to_json())
        assert s2.index == 3
        assert s2.description == "Added bridge"
        assert s2.author == "j@e.com"

    def test_milestone_round_trip(self):
        s = self._make(milestone=MilestoneTag.FINAL_MIX)
        s2 = Snapshot.from_json(s.to_json())
        assert s2.milestone == MilestoneTag.FINAL_MIX

    def test_no_milestone_round_trip(self):
        s = self._make()
        s2 = Snapshot.from_json(s.to_json())
        assert s2.milestone is None

    def test_media_round_trip(self):
        entry = ManifestEntry(
            original_name="Guitar.aif",
            content_hash="abc123",
            size_bytes=1000,
        )
        s = self._make(media=[entry])
        s2 = Snapshot.from_json(s.to_json())
        assert len(s2.media) == 1
        assert s2.media[0].original_name == "Guitar.aif"

    def test_folder_name(self):
        assert self._make(index=7).folder_name == "007"
        assert self._make(index=42).folder_name == "042"

    def test_display_index(self):
        assert self._make(index=3).display_index == "v3"


# ─────────────────────────────────────────────────────────────
# Handoff serialization
# ─────────────────────────────────────────────────────────────

class TestHandoffSerialization:
    def test_open_factory(self):
        h = Handoff.open()
        assert h.lock_state == LockState.OPEN
        assert h.active_editor is None

    def test_round_trip(self):
        h = Handoff(
            active_editor="a@e.com",
            since=datetime.now(timezone.utc),
            note="Bridge needs work",
            snapshot_index=5,
            lock_state=LockState.LOCKED,
        )
        h2 = Handoff.from_json(h.to_json())
        assert h2.active_editor == "a@e.com"
        assert h2.lock_state == LockState.LOCKED
        assert h2.note == "Bridge needs work"
        assert h2.snapshot_index == 5


# ─────────────────────────────────────────────────────────────
# StorageProvider
# ─────────────────────────────────────────────────────────────

class TestStorageProvider:
    def test_local_factory(self, tmp_path):
        p = StorageProvider.local(tmp_path)
        assert p.provider_type == StorageProviderType.LOCAL
        assert p.is_syncing is True

    def test_detect_icloud(self, tmp_path):
        icloud = tmp_path / "Mobile Documents" / "BandTracker"
        p = StorageProvider.detect(icloud)
        assert p.provider_type == StorageProviderType.ICLOUD

    def test_detect_dropbox(self, tmp_path):
        dropbox = tmp_path / "Dropbox" / "BandTracker"
        p = StorageProvider.detect(dropbox)
        assert p.provider_type == StorageProviderType.DROPBOX

    def test_detect_local(self, tmp_path):
        local = tmp_path / "BandTracker"
        p = StorageProvider.detect(local)
        assert p.provider_type == StorageProviderType.LOCAL

    def test_projects_path(self, tmp_path):
        p = StorageProvider.local(tmp_path)
        assert p.projects_path == tmp_path / "projects"

    def test_project_path(self, tmp_path):
        p = StorageProvider.local(tmp_path)
        assert p.project_path("Song") == tmp_path / "projects" / "Song"


# ─────────────────────────────────────────────────────────────
# ProjectPaths
# ─────────────────────────────────────────────────────────────

class TestProjectPaths:
    def test_all_paths(self, tmp_path):
        paths = ProjectPaths(tmp_path)
        assert paths.live == tmp_path / "live"
        assert paths.media == tmp_path / "media"
        assert paths.snapshots == tmp_path / "snapshots"
        assert paths.docs == tmp_path / "docs"
        assert paths.project_json == tmp_path / "project.json"
        assert paths.handoff_json == tmp_path / "handoff.json"
        assert paths.noise_mask_json == tmp_path / "noise_mask.json"

    def test_live_band(self, tmp_path):
        paths = ProjectPaths(tmp_path)
        assert paths.live_band("Song") == tmp_path / "live" / "Song.band"

    def test_live_project_data(self, tmp_path):
        paths = ProjectPaths(tmp_path)
        expected = tmp_path / "live" / "Song.band" / "Alternatives" / "000" / "ProjectData"
        assert paths.live_project_data("Song") == expected

    def test_snapshot_paths(self, tmp_path):
        paths = ProjectPaths(tmp_path)
        assert paths.snapshot(3) == tmp_path / "snapshots" / "003"
        assert paths.snapshot_project_data(3) == tmp_path / "snapshots" / "003" / "ProjectData"
        assert paths.snapshot_meta(3) == tmp_path / "snapshots" / "003" / "meta.json"
        assert paths.snapshot_manifest(3) == tmp_path / "snapshots" / "003" / "manifest.json"
        assert paths.snapshot_sidecar(3) == tmp_path / "snapshots" / "003" / "sidecar"

    def test_all_snapshot_indices_empty(self, tmp_path):
        paths = ProjectPaths(tmp_path)
        assert paths.all_snapshot_indices() == []

    def test_all_snapshot_indices_sorted(self, tmp_path):
        paths = ProjectPaths(tmp_path)
        for i in [3, 1, 7, 2]:
            (tmp_path / "snapshots" / f"{i:03d}").mkdir(parents=True)
        assert paths.all_snapshot_indices() == [1, 2, 3, 7]
