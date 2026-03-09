"""
core/models.py

The data model for BandTracker. These dataclasses are the contract
between all modules — snapshot writer, diff engine, reconciler,
handoff, watcher, and eventually the Swift UI layer.

Nothing in here does any I/O. These are pure data structures.
Serialization to/from JSON is handled here so the rest of the
codebase never has to think about it.

Storage independence: nothing in these models assumes where the
project folder lives. Paths are always resolved by the storage
provider and passed in — never constructed from assumptions inside
these classes.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional


# ─────────────────────────────────────────────────────────────
# ENUMS
# ─────────────────────────────────────────────────────────────

class MilestoneTag(str, Enum):
    """
    Special tags that anchor the timeline visually and semantically.
    A snapshot can have at most one milestone tag.
    """
    ARRANGEMENT_LOCK = "arrangement_lock"   # arrangement is frozen
    FINAL_MIX        = "final_mix"          # this is the one
    HANDOFF          = "handoff"            # explicitly passed to collaborator


class StorageProviderType(str, Enum):
    """
    Identifies which storage backend the project root lives on.
    BandTracker's core logic never branches on this — it's metadata
    for the UI layer to display sync status and for future provider
    implementations to self-identify.
    """
    LOCAL    = "local"
    ICLOUD   = "icloud"
    DROPBOX  = "dropbox"
    GOOGLE   = "google_drive"
    UNKNOWN  = "unknown"


class LockState(str, Enum):
    """
    Who currently has the right to make changes to this project.
    OPEN means no collaborator is set — solo use.
    """
    OPEN   = "open"    # no handoff in progress, single user
    LOCKED = "locked"  # a specific collaborator has the ball


# ─────────────────────────────────────────────────────────────
# STORAGE PROVIDER
# ─────────────────────────────────────────────────────────────

@dataclass
class StorageProvider:
    """
    Describes where the BandTracker root folder lives and whether
    sync is active. This is the only place in the codebase that
    knows about storage backends.

    Future providers (iCloud, Dropbox, etc.) will subclass or
    configure this — the rest of the codebase just calls
    provider.root_path and never looks at provider_type.

    Fields:
        provider_type   which backend (local, icloud, dropbox, ...)
        root_path       absolute path to the BandTracker root folder
                        e.g. ~/BandTracker or ~/Dropbox/BandTracker
        is_syncing      whether the provider reports active sync
                        always True for local (vacuously)
    """
    provider_type: StorageProviderType
    root_path: Path
    is_syncing: bool = True

    @property
    def projects_path(self) -> Path:
        return self.root_path / "projects"

    def project_path(self, project_name: str) -> Path:
        return self.projects_path / project_name

    @classmethod
    def local(cls, root_path: Path) -> StorageProvider:
        return cls(
            provider_type=StorageProviderType.LOCAL,
            root_path=root_path,
            is_syncing=True,
        )

    @classmethod
    def detect(cls, root_path: Path) -> StorageProvider:
        """
        Inspect root_path to guess which sync provider manages it.
        Used during onboarding when the user points BandTracker at
        an existing folder.
        """
        path_str = str(root_path).lower()

        if "mobile documents" in path_str or "icloud" in path_str:
            return cls(StorageProviderType.ICLOUD, root_path)
        if "dropbox" in path_str:
            return cls(StorageProviderType.DROPBOX, root_path)
        if "google drive" in path_str or "googledrive" in path_str:
            return cls(StorageProviderType.GOOGLE, root_path)

        return cls(StorageProviderType.LOCAL, root_path)


# ─────────────────────────────────────────────────────────────
# COLLABORATOR
# ─────────────────────────────────────────────────────────────

@dataclass
class Collaborator:
    """
    A person who has access to a project.

    display_name    human-readable name shown in the timeline
    identifier      email or Apple ID — used to match handoff.json
                    entries across machines. Not a login — just a
                    stable string both machines agree on.
    """
    display_name: str
    identifier: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Collaborator:
        return cls(**d)


# ─────────────────────────────────────────────────────────────
# MANIFEST ENTRY
# ─────────────────────────────────────────────────────────────

@dataclass
class ManifestEntry:
    """
    One audio file as it existed at the time of a snapshot.

    original_name   the filename GarageBand uses inside Media/Audio Files/
                    e.g. "Guitar Take 1.aif"
    content_hash    sha256 of the file contents at snapshot time
                    this is the key into the project's media/ store
    size_bytes      file size — useful for storage reporting and
                    progress indicators during restore
    """
    original_name: str
    content_hash: str
    size_bytes: int

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> ManifestEntry:
        return cls(**d)


# ─────────────────────────────────────────────────────────────
# SNAPSHOT
# ─────────────────────────────────────────────────────────────

@dataclass
class Snapshot:
    """
    A named point in the project's history.

    index           1-based integer, monotonically increasing,
                    never reused even if snapshots are deleted
    description     human-readable summary of what changed
                    either typed by the musician or auto-generated
                    by the diff engine
    timestamp       UTC datetime of when the snapshot was taken
    author          identifier of the collaborator who saved it
    diff_summary    list of decoded change descriptions from the
                    diff engine e.g. ["tempo changed to 124 BPM",
                    "2 tracks added"]. Empty for the initial snapshot.
    milestone       optional MilestoneTag — arrangement lock, final
                    mix, or handoff. At most one per snapshot.
    media           list of ManifestEntry for every audio file that
                    existed in the project at this snapshot
    sidecar_files   list of filenames attached to this snapshot
                    e.g. ["bounce.m4a", "notes.md", "lyrics.txt"]
                    actual files live in snapshots/{index}/sidecar/
    """
    index: int
    description: str
    timestamp: datetime
    author: str
    diff_summary: list[str] = field(default_factory=list)
    milestone: Optional[MilestoneTag] = None
    media: list[ManifestEntry] = field(default_factory=list)
    sidecar_files: list[str] = field(default_factory=list)

    @property
    def folder_name(self) -> str:
        """Zero-padded folder name e.g. '007'"""
        return f"{self.index:03d}"

    @property
    def display_index(self) -> str:
        return f"v{self.index}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        d["milestone"] = self.milestone.value if self.milestone else None
        d["media"] = [m.to_dict() for m in self.media]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Snapshot:
        d = d.copy()
        d["timestamp"] = datetime.fromisoformat(d["timestamp"])
        d["milestone"] = MilestoneTag(d["milestone"]) if d["milestone"] else None
        d["media"] = [ManifestEntry.from_dict(m) for m in d.get("media", [])]
        return cls(**d)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_json(cls, s: str) -> Snapshot:
        return cls.from_dict(json.loads(s))


# ─────────────────────────────────────────────────────────────
# HANDOFF
# ─────────────────────────────────────────────────────────────

@dataclass
class Handoff:
    """
    The live coordination file. Tells both machines who currently
    has the right to make changes and when that started.

    This file changes independently of snapshots — it is not
    historical data. Only the current state is stored here.
    The history of who had the ball when is derivable from the
    snapshot author fields.

    active_editor   identifier of who currently has the ball
                    None means the project is OPEN (solo use or
                    no handoff has happened yet)
    since           when the current editor took the ball
    note            optional message from the person handing off
                    e.g. "Bridge needs work, leave drums alone"
    snapshot_index  which snapshot was current when the handoff
                    happened — used for conflict detection
    lock_state      OPEN or LOCKED
    """
    active_editor: Optional[str]
    since: datetime
    note: Optional[str] = None
    snapshot_index: Optional[int] = None
    lock_state: LockState = LockState.OPEN

    def to_dict(self) -> dict:
        return {
            "active_editor": self.active_editor,
            "since": self.since.isoformat(),
            "note": self.note,
            "snapshot_index": self.snapshot_index,
            "lock_state": self.lock_state.value,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Handoff:
        return cls(
            active_editor=d.get("active_editor"),
            since=datetime.fromisoformat(d["since"]),
            note=d.get("note"),
            snapshot_index=d.get("snapshot_index"),
            lock_state=LockState(d.get("lock_state", "open")),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_json(cls, s: str) -> Handoff:
        return cls.from_dict(json.loads(s))

    @classmethod
    def open(cls) -> Handoff:
        """Factory for a new unlocked handoff state."""
        return cls(
            active_editor=None,
            since=datetime.now(timezone.utc),
            lock_state=LockState.OPEN,
        )


# ─────────────────────────────────────────────────────────────
# PROJECT
# ─────────────────────────────────────────────────────────────

@dataclass
class Project:
    """
    The top-level record for a tracked GarageBand project.

    name                human-readable display name e.g. "Midnight Drive"
                        also used as the folder name on disk
    uuid                internal stable identity — survives renames
                        never shown to the musician
    created_at          when BandTracker first started tracking this
    owner               identifier of whoever ran `init`
    collaborators       everyone with access, including the owner
    garageband_version  GB version string at creation time
                        used to warn about version mismatches
    latest_snapshot     index of the most recent snapshot
                        None if no snapshots yet (shouldn't happen
                        in practice — init always takes snapshot 1)
    next_snapshot_index monotonically increasing counter — never
                        reuse an index even if snapshots are deleted
    gb_bundle_path      path to the GarageBand .band bundle that GB
                        saves to, stored as a string (~/... when
                        possible for cross-machine readability).
                        None for projects initialized before Increment 5
                        — run `bandtracker set-gb` to populate.
    gb_bundle_alias     base64-encoded macOS NSURL bookmark for the
                        GB bundle. Enables silent resolution if the
                        file is moved within the same volume.
                        None on non-macOS or when PyObjC is unavailable.
                        Also None for pre-Increment-5 projects until
                        `set-gb` is run.
    """
    name: str
    uuid: str
    created_at: datetime
    owner: str
    collaborators: list[Collaborator] = field(default_factory=list)
    garageband_version: Optional[str] = None
    latest_snapshot: Optional[int] = None
    next_snapshot_index: int = 1
    gb_bundle_path: Optional[str] = None
    gb_bundle_alias: Optional[str] = None

    @classmethod
    def create(cls, name: str, owner_identifier: str,
               owner_display_name: str,
               garageband_version: Optional[str] = None,
               gb_bundle_path: Optional[str] = None,
               gb_bundle_alias: Optional[str] = None) -> Project:
        """Factory for a brand new project."""
        owner = Collaborator(
            display_name=owner_display_name,
            identifier=owner_identifier,
        )
        return cls(
            name=name,
            uuid=str(uuid.uuid4()),
            created_at=datetime.now(timezone.utc),
            owner=owner_identifier,
            collaborators=[owner],
            garageband_version=garageband_version,
            gb_bundle_path=gb_bundle_path,
            gb_bundle_alias=gb_bundle_alias,
        )

    def get_collaborator(self, identifier: str) -> Optional[Collaborator]:
        return next(
            (c for c in self.collaborators if c.identifier == identifier),
            None,
        )

    def add_collaborator(self, collaborator: Collaborator):
        if not self.get_collaborator(collaborator.identifier):
            self.collaborators.append(collaborator)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "uuid": self.uuid,
            "created_at": self.created_at.isoformat(),
            "owner": self.owner,
            "collaborators": [c.to_dict() for c in self.collaborators],
            "garageband_version": self.garageband_version,
            "latest_snapshot": self.latest_snapshot,
            "next_snapshot_index": self.next_snapshot_index,
            "gb_bundle_path": self.gb_bundle_path,
            "gb_bundle_alias": self.gb_bundle_alias,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Project:
        d = d.copy()
        d["created_at"] = datetime.fromisoformat(d["created_at"])
        d["collaborators"] = [
            Collaborator.from_dict(c) for c in d.get("collaborators", [])
        ]
        # Backward-compatible: old project.json files won't have these fields
        d.setdefault("gb_bundle_path", None)
        d.setdefault("gb_bundle_alias", None)
        return cls(**d)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_json(cls, s: str) -> Project:
        return cls.from_dict(json.loads(s))


# ─────────────────────────────────────────────────────────────
# PROJECT PATHS
# ─────────────────────────────────────────────────────────────

@dataclass
class ProjectPaths:
    """
    All the path conventions for a project in one place.
    Nothing in BandTracker constructs project paths ad-hoc —
    everything goes through this class.

    This is the single place to update if the folder structure
    ever needs to change.

    Usage:
        paths = ProjectPaths(provider.project_path("MidnightDrive"))
        paths.live_band      # ~/BandTracker/projects/MidnightDrive/live/MidnightDrive.band
        paths.media          # ~/BandTracker/projects/MidnightDrive/media/
        paths.snapshot(3)    # ~/BandTracker/projects/MidnightDrive/snapshots/003/
    """
    project_root: Path

    @property
    def live(self) -> Path:
        return self.project_root / "live"

    @property
    def media(self) -> Path:
        return self.project_root / "media"

    @property
    def snapshots(self) -> Path:
        return self.project_root / "snapshots"

    @property
    def docs(self) -> Path:
        return self.project_root / "docs"

    @property
    def project_json(self) -> Path:
        return self.project_root / "project.json"

    @property
    def handoff_json(self) -> Path:
        return self.project_root / "handoff.json"

    @property
    def noise_mask_json(self) -> Path:
        return self.project_root / "noise_mask.json"

    def live_band(self, project_name: str) -> Path:
        return self.live / f"{project_name}.band"

    def live_project_data(self, project_name: str) -> Path:
        return self.live_band(project_name) / "Alternatives" / "000" / "ProjectData"

    def live_media_dir(self, project_name: str) -> Path:
        return self.live_band(project_name) / "Media" / "Audio Files"

    def snapshot(self, index: int) -> Path:
        return self.snapshots / f"{index:03d}"

    def snapshot_project_data(self, index: int) -> Path:
        return self.snapshot(index) / "ProjectData"

    def snapshot_meta(self, index: int) -> Path:
        return self.snapshot(index) / "meta.json"

    def snapshot_manifest(self, index: int) -> Path:
        return self.snapshot(index) / "manifest.json"

    def snapshot_sidecar(self, index: int) -> Path:
        return self.snapshot(index) / "sidecar"

    def media_file(self, content_hash: str, suffix: str = ".aif") -> Path:
        return self.media / f"{content_hash}{suffix}"

    def all_snapshot_indices(self) -> list[int]:
        """Return sorted list of snapshot indices that exist on disk."""
        if not self.snapshots.exists():
            return []
        indices = []
        for d in self.snapshots.iterdir():
            if d.is_dir() and d.name.isdigit():
                indices.append(int(d.name))
        return sorted(indices)
