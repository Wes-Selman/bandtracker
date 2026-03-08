"""
core/init.py  —  Increment 1: Project initialization

Accepts a path to an existing .band bundle and sets up the full
BandTracker folder structure for that project.

Public API:
    initialize(band_path, provider, owner_identifier, owner_display_name)
        → InitResult

    validate_band(band_path)
        → ValidationResult  (can be called standalone before init)
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.models import (
    Handoff,
    ManifestEntry,
    Project,
    ProjectPaths,
    Snapshot,
    StorageProvider,
)


# ─────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────

PROJECTDATA_MAGIC = b"gnoS"

# Audio file extensions GarageBand produces
AUDIO_EXTENSIONS = {".aif", ".aiff", ".caf", ".wav", ".m4a", ".mp3"}

# GarageBand writes a lock file while saving
GARAGEBAND_LOCK_NAMES = {".DocumentRevisions-V100", ".com.apple.GarageBand.lock"}

# Minimum free space required before copying (bytes) — 500MB headroom
MIN_FREE_SPACE_BYTES = 500 * 1024 * 1024


# ─────────────────────────────────────────────────────────────
# RESULT TYPES
# ─────────────────────────────────────────────────────────────

@dataclass
class ValidationResult:
    """
    Result of validating a .band bundle before initialization.
    Check .ok before using any other fields.
    """
    ok: bool
    band_path: Path
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    project_data_path: Optional[Path] = None
    media_files: list[Path] = field(default_factory=list)
    total_size_bytes: int = 0

    def add_error(self, msg: str):
        self.errors.append(msg)
        self.ok = False

    def add_warning(self, msg: str):
        self.warnings.append(msg)


@dataclass
class InitResult:
    """
    Result of an initialization attempt.
    Check .ok before using any other fields.
    """
    ok: bool
    project_name: str
    project_root: Path
    snapshot_index: int = 1
    media_files_copied: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def project_json_path(self) -> Path:
        return self.project_root / "project.json"

    @property
    def first_snapshot_path(self) -> Path:
        return self.project_root / "snapshots" / "001"


# ─────────────────────────────────────────────────────────────
# VALIDATION
# ─────────────────────────────────────────────────────────────

def validate_band(band_path: Path) -> ValidationResult:
    """
    Validate a .band bundle before initialization.
    Does not modify anything on disk.

    Checks:
      - path exists and is a directory
      - has .band extension
      - contains Output/ProjectData
      - ProjectData starts with gnoS magic bytes
      - GarageBand does not currently have it open
      - sufficient free disk space for copying
    """
    result = ValidationResult(ok=True, band_path=band_path)

    # ── Existence and type
    if not band_path.exists():
        result.add_error(f"Path does not exist: {band_path}")
        return result

    if not band_path.is_dir():
        result.add_error(f"Not a directory (expected a .band bundle): {band_path}")
        return result

    if band_path.suffix.lower() != ".band":
        result.add_warning(
            f"Path does not have a .band extension: {band_path.name}. "
            "Proceeding anyway."
        )

    # ── ProjectData
    pd_path = band_path / "Output" / "ProjectData"
    if not pd_path.exists():
        result.add_error(
            f"No Output/ProjectData found in {band_path.name}. "
            "Is this a valid GarageBand project?"
        )
        return result

    result.project_data_path = pd_path

    # ── Magic bytes
    try:
        magic = pd_path.read_bytes()[:4]
        if magic != PROJECTDATA_MAGIC:
            result.add_error(
                f"ProjectData does not start with expected magic bytes "
                f"(got {magic.hex()!r}, expected {PROJECTDATA_MAGIC.hex()!r}). "
                "This may not be a GarageBand 10.4+ project."
            )
    except OSError as e:
        result.add_error(f"Could not read ProjectData: {e}")
        return result

    # ── GarageBand lock check
    for lock_name in GARAGEBAND_LOCK_NAMES:
        if (band_path / lock_name).exists():
            result.add_error(
                f"GarageBand appears to have this project open "
                f"(found {lock_name}). Close GarageBand and try again."
            )

    # ── Collect media files
    media_dir = band_path / "Media" / "Audio Files"
    if media_dir.exists():
        for f in media_dir.rglob("*"):
            if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS:
                result.media_files.append(f)

    # ── Total size (for disk space check)
    total = 0
    for f in band_path.rglob("*"):
        if f.is_file():
            try:
                total += f.stat().st_size
            except OSError:
                pass
    result.total_size_bytes = total

    # ── Disk space
    try:
        free = shutil.disk_usage(band_path).free
        needed = total + MIN_FREE_SPACE_BYTES
        if free < needed:
            result.add_error(
                f"Insufficient disk space. Need ~{needed // (1024*1024)}MB, "
                f"have {free // (1024*1024)}MB free."
            )
    except OSError:
        result.add_warning("Could not check available disk space.")

    return result


# ─────────────────────────────────────────────────────────────
# NAME SANITIZATION
# ─────────────────────────────────────────────────────────────

def sanitize_project_name(band_path: Path) -> str:
    """
    Derive a clean project name from the .band filename.

    Rules:
      - Strip the .band extension
      - Replace runs of whitespace with a single space
      - Strip leading/trailing whitespace
      - Remove characters unsafe in folder names
      - Collapse to "Untitled Project" if nothing remains
    """
    name = band_path.stem

    # Remove unsafe folder name characters
    # Keep: letters, digits, spaces, hyphens, underscores, apostrophes, parens
    name = re.sub(r"[^\w\s\-'()]", "", name)

    # Normalize whitespace
    name = re.sub(r"\s+", " ", name).strip()

    if not name:
        name = "Untitled Project"

    return name


# ─────────────────────────────────────────────────────────────
# MEDIA HASHING
# ─────────────────────────────────────────────────────────────

def hash_file(path: Path) -> str:
    """SHA-256 hash of a file's contents. Returns hex string."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def copy_media_to_store(
    media_files: list[Path],
    media_store: Path,
) -> list[ManifestEntry]:
    """
    Hash each media file and copy into the content-addressed store.
    Skips files already present (deduplication).
    Returns a ManifestEntry for every file — errors are embedded in
    the entry rather than raised, so one bad file doesn't abort init.
    """
    media_store.mkdir(parents=True, exist_ok=True)
    entries = []

    for src in media_files:
        try:
            content_hash = hash_file(src)
            suffix = src.suffix.lower()
            dest = media_store / f"{content_hash}{suffix}"

            if not dest.exists():
                # Write to temp then rename — atomic on POSIX
                tmp = dest.with_suffix(".tmp")
                shutil.copy2(src, tmp)
                tmp.rename(dest)

            entries.append(ManifestEntry(
                original_name=src.name,
                content_hash=content_hash,
                size_bytes=src.stat().st_size,
            ))
        except OSError as e:
            entries.append(ManifestEntry(
                original_name=src.name,
                content_hash=f"ERROR:{e}",
                size_bytes=0,
            ))

    return entries


# ─────────────────────────────────────────────────────────────
# FOLDER STRUCTURE
# ─────────────────────────────────────────────────────────────

def create_folder_structure(paths: ProjectPaths):
    """
    Create all required directories for a new project.
    Safe to call on a partially-created structure.
    """
    for d in [
        paths.live,
        paths.media,
        paths.snapshots,
        paths.docs,
        paths.snapshot_sidecar(1),
    ]:
        d.mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────────────────────────
# COPY BAND BUNDLE
# ─────────────────────────────────────────────────────────────

def copy_band_bundle(src: Path, dest_dir: Path, dest_name: str = "") -> Path:
    """
    Copy a .band bundle into dest_dir, optionally renaming it.
    If dest_name is provided, the copy will be named {dest_name}.band
    (used to normalize filenames when the original has unsafe characters).
    Verifies the copy before returning.
    Cleans up and raises OSError if verification fails.
    """
    final_name = f"{dest_name}.band" if dest_name else src.name
    dest = dest_dir / final_name

    if dest.exists():
        shutil.rmtree(dest)

    shutil.copytree(src, dest)

    # Verify ProjectData survived the copy
    pd = dest / "Output" / "ProjectData"
    if not pd.exists():
        shutil.rmtree(dest)
        raise OSError(
            f"Copy verification failed: ProjectData missing in {dest}"
        )

    magic = pd.read_bytes()[:4]
    if magic != PROJECTDATA_MAGIC:
        shutil.rmtree(dest)
        raise OSError(
            f"Copy verification failed: ProjectData magic corrupt in {dest}"
        )

    return dest


# ─────────────────────────────────────────────────────────────
# SNAPSHOT 001
# ─────────────────────────────────────────────────────────────

def write_initial_snapshot(
    paths: ProjectPaths,
    project_name: str,
    author: str,
    media_entries: list[ManifestEntry],
) -> Snapshot:
    """
    Write snapshot 001 — the "Initial version" taken at init time.
    No diff_summary (nothing to compare against yet).
    """
    snap_dir = paths.snapshot(1)
    snap_dir.mkdir(parents=True, exist_ok=True)

    # Copy ProjectData from live/ into snapshot/
    src_pd = paths.live_project_data(project_name)
    dest_pd = paths.snapshot_project_data(1)
    shutil.copy2(src_pd, dest_pd)

    snapshot = Snapshot(
        index=1,
        description="Initial version",
        timestamp=datetime.now(timezone.utc),
        author=author,
        diff_summary=[],
        milestone=None,
        media=media_entries,
        sidecar_files=[],
    )

    paths.snapshot_meta(1).write_text(snapshot.to_json())

    manifest = {"entries": [e.to_dict() for e in media_entries]}
    paths.snapshot_manifest(1).write_text(json.dumps(manifest, indent=2))

    return snapshot


# ─────────────────────────────────────────────────────────────
# MAIN ENTRY POINT
# ─────────────────────────────────────────────────────────────

def initialize(
    band_path: Path,
    provider: StorageProvider,
    owner_identifier: str,
    owner_display_name: str,
) -> InitResult:
    """
    Initialize BandTracker tracking for a .band project.

    Steps:
      1. Validate the bundle
      2. Determine and sanitize project name
      3. Check for name collision
      4. Create folder structure
      5. Copy bundle into live/
      6. Hash and copy media into media/
      7. Write project.json and handoff.json
      8. Write snapshot 001

    On failure, any partially-created structure is cleaned up.
    """
    # ── 1. Validate
    validation = validate_band(band_path)
    if not validation.ok:
        return InitResult(
            ok=False,
            project_name="",
            project_root=Path("."),
            errors=validation.errors,
        )

    # ── 2. Project name
    project_name = sanitize_project_name(band_path)

    # ── 3. Name collision
    project_root = provider.project_path(project_name)
    if project_root.exists() and (project_root / "project.json").exists():
        return InitResult(
            ok=False,
            project_name=project_name,
            project_root=project_root,
            errors=[
                f"A project named '{project_name}' is already tracked at "
                f"{project_root}. Rename the .band file and try again, "
                f"or use --name to specify a different name."
            ],
        )

    paths = ProjectPaths(project_root)

    # ── 4. Folder structure
    try:
        provider.projects_path.mkdir(parents=True, exist_ok=True)
        create_folder_structure(paths)
    except OSError as e:
        return InitResult(
            ok=False,
            project_name=project_name,
            project_root=project_root,
            errors=[f"Could not create folder structure: {e}"],
        )

    # ── 5. Copy bundle (rename to sanitized project name)
    try:
        copy_band_bundle(band_path, paths.live, dest_name=project_name)
    except OSError as e:
        shutil.rmtree(project_root, ignore_errors=True)
        return InitResult(
            ok=False,
            project_name=project_name,
            project_root=project_root,
            errors=[f"Could not copy .band bundle: {e}"],
        )

    # ── 6. Media
    media_entries = copy_media_to_store(validation.media_files, paths.media)
    failed_media = [e for e in media_entries if e.content_hash.startswith("ERROR:")]
    good_media = [e for e in media_entries if not e.content_hash.startswith("ERROR:")]

    # ── 7. project.json and handoff.json
    project = Project.create(
        name=project_name,
        owner_identifier=owner_identifier,
        owner_display_name=owner_display_name,
    )
    project.latest_snapshot = 1
    project.next_snapshot_index = 2

    paths.project_json.write_text(project.to_json())
    paths.handoff_json.write_text(Handoff.open().to_json())

    # ── 8. Snapshot 001
    try:
        write_initial_snapshot(
            paths=paths,
            project_name=project_name,
            author=owner_identifier,
            media_entries=good_media,
        )
    except OSError as e:
        shutil.rmtree(project_root, ignore_errors=True)
        return InitResult(
            ok=False,
            project_name=project_name,
            project_root=project_root,
            errors=[f"Could not write initial snapshot: {e}"],
        )

    return InitResult(
        ok=True,
        project_name=project_name,
        project_root=project_root,
        snapshot_index=1,
        media_files_copied=len(good_media),
        errors=[
            f"Warning: could not copy '{e.original_name}': "
            f"{e.content_hash.replace('ERROR:', '')}"
            for e in failed_media
        ],
    )
