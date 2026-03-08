"""
core/snapshot.py

Snapshot writer for BandTracker — Increment 2.

Takes a named snapshot of the live project:
  - Deduplicates media into media/
  - Writes ProjectData copy, manifest.json, meta.json atomically
  - Updates project.json (latest_snapshot, next_snapshot_index)
  - Supports optional milestone tags
  - Cleans up the snapshot folder on any failure

Nothing in here touches the CLI. Nothing constructs paths ad-hoc.
All path logic lives in ProjectPaths.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.models import (
    ManifestEntry,
    MilestoneTag,
    Project,
    ProjectPaths,
    Snapshot,
    StorageProvider,
)
from core.init import copy_media_to_store, hash_file


# ─────────────────────────────────────────────────────────────
# RESULT
# ─────────────────────────────────────────────────────────────

@dataclass
class SnapshotResult:
    """
    Return value from take_snapshot().
    Always check .ok before using other fields.
    """
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # Populated on success
    snapshot_index: Optional[int] = None
    snapshot_path: Optional[Path] = None
    project_name: Optional[str] = None
    media_files_deduped: int = 0    # files already in store (skipped)
    media_files_copied: int = 0     # files newly written to store
    description: Optional[str] = None


# ─────────────────────────────────────────────────────────────
# INTERNAL HELPERS
# ─────────────────────────────────────────────────────────────

def _load_project(paths: ProjectPaths) -> tuple[Optional[Project], str]:
    """Load project.json. Returns (project, error_message)."""
    if not paths.project_json.exists():
        return None, f"project.json not found at {paths.project_json}"
    try:
        return Project.from_json(paths.project_json.read_text()), ""
    except Exception as e:
        return None, f"Could not parse project.json: {e}"


def _collect_live_media(paths: ProjectPaths, project_name: str) -> list[Path]:
    """
    Return all audio files currently in the live bundle's Media/Audio Files/.
    Returns empty list if the directory doesn't exist (project has no media).
    """
    media_dir = paths.live_media_dir(project_name)
    if not media_dir.exists():
        return []
    return [f for f in media_dir.iterdir() if f.is_file()]


def _ensure_media_in_store(
    media_files: list[Path],
    media_store: Path,
) -> tuple[list[ManifestEntry], int, int]:
    """
    Ensure every audio file is in the content-addressed media store.
    Skips files whose hash already exists in the store (deduplication).

    Returns:
        entries         list of ManifestEntry for the manifest
        copied_count    files newly written to the store
        deduped_count   files already present (skipped)
    """
    media_store.mkdir(parents=True, exist_ok=True)
    entries: list[ManifestEntry] = []
    copied_count = 0
    deduped_count = 0

    for f in media_files:
        try:
            content_hash = hash_file(f)
            size_bytes = f.stat().st_size
            suffix = f.suffix or ".aif"
            dest = media_store / f"{content_hash}{suffix}"

            if dest.exists():
                deduped_count += 1
            else:
                shutil.copy2(f, dest)
                copied_count += 1

            entries.append(ManifestEntry(
                original_name=f.name,
                content_hash=content_hash,
                size_bytes=size_bytes,
            ))
        except Exception as e:
            # Record the error in the entry rather than aborting — matches
            # the pattern established in copy_media_to_store in init.py.
            entries.append(ManifestEntry(
                original_name=f.name,
                content_hash=f"ERROR: {e}",
                size_bytes=0,
            ))

    return entries, copied_count, deduped_count


def _write_snapshot_atomically(
    paths: ProjectPaths,
    index: int,
    project_name: str,
    description: str,
    author: str,
    media_entries: list[ManifestEntry],
    milestone: Optional[MilestoneTag],
) -> Snapshot:
    """
    Write ProjectData, manifest.json, and meta.json into the snapshot
    folder. The sidecar/ subdirectory is created but left empty.

    Raises on any I/O failure — caller is responsible for cleanup.
    """
    snap_dir = paths.snapshot(index)
    snap_dir.mkdir(parents=True, exist_ok=True)
    paths.snapshot_sidecar(index).mkdir(parents=True, exist_ok=True)

    # 1. Copy ProjectData from live/
    live_pd = paths.live_project_data(project_name)
    snap_pd = paths.snapshot_project_data(index)
    shutil.copy2(live_pd, snap_pd)

    # 2. Build Snapshot object
    snap = Snapshot(
        index=index,
        description=description,
        timestamp=datetime.now(timezone.utc),
        author=author,
        diff_summary=[],
        milestone=milestone,
        media=media_entries,
        sidecar_files=[],
    )

    # 3. Write manifest.json
    manifest_data = [e.to_dict() for e in media_entries]
    paths.snapshot_manifest(index).write_text(
        json.dumps(manifest_data, indent=2)
    )

    # 4. Write meta.json
    paths.snapshot_meta(index).write_text(snap.to_json())

    return snap


def _update_project_json(paths: ProjectPaths, index: int) -> None:
    """
    Advance latest_snapshot and next_snapshot_index in project.json.
    Raises on I/O or parse failure.
    """
    project = Project.from_json(paths.project_json.read_text())
    project.latest_snapshot = index
    project.next_snapshot_index = index + 1
    paths.project_json.write_text(project.to_json())


# ─────────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────────

PLACEHOLDER_DESCRIPTION = "Work in progress"


def take_snapshot(
    provider: StorageProvider,
    project_name: str,
    author: str,
    message: Optional[str] = None,
    milestone: Optional[MilestoneTag] = None,
) -> SnapshotResult:
    """
    Take a named snapshot of the live project.

    Args:
        provider        storage provider (knows where projects live)
        project_name    name of the project folder (sanitized)
        author          identifier of whoever is running this command
        message         optional description; placeholder used if None
        milestone       optional MilestoneTag to attach

    Returns:
        SnapshotResult with ok=True on success, errors populated on failure
    """
    project_root = provider.project_path(project_name)
    paths = ProjectPaths(project_root)

    # ── Pre-flight checks ──────────────────────────────────────
    project, load_err = _load_project(paths)
    if load_err:
        return SnapshotResult(ok=False, errors=[load_err])

    live_band = paths.live_band(project_name)
    if not live_band.exists():
        return SnapshotResult(
            ok=False,
            errors=[f"Live bundle not found: {live_band}"]
        )

    live_pd = paths.live_project_data(project_name)
    if not live_pd.exists():
        return SnapshotResult(
            ok=False,
            errors=[f"ProjectData missing from live bundle: {live_pd}"]
        )

    index = project.next_snapshot_index
    description = message if message else PLACEHOLDER_DESCRIPTION
    snap_dir = paths.snapshot(index)

    # ── Media deduplication ────────────────────────────────────
    live_media = _collect_live_media(paths, project_name)
    try:
        media_entries, copied, deduped = _ensure_media_in_store(
            live_media, paths.media
        )
    except Exception as e:
        return SnapshotResult(
            ok=False,
            errors=[f"Media store error: {e}"]
        )

    # ── Atomic snapshot write ──────────────────────────────────
    try:
        snap = _write_snapshot_atomically(
            paths=paths,
            index=index,
            project_name=project_name,
            description=description,
            author=author,
            media_entries=media_entries,
            milestone=milestone,
        )
    except Exception as e:
        # Clean up the partial snapshot folder
        if snap_dir.exists():
            shutil.rmtree(snap_dir, ignore_errors=True)
        return SnapshotResult(
            ok=False,
            errors=[f"Failed to write snapshot: {e}"]
        )

    # ── Update project.json ────────────────────────────────────
    try:
        _update_project_json(paths, index)
    except Exception as e:
        # Roll back the snapshot folder — project.json is the source of truth
        if snap_dir.exists():
            shutil.rmtree(snap_dir, ignore_errors=True)
        return SnapshotResult(
            ok=False,
            errors=[f"Failed to update project.json: {e}"]
        )

    return SnapshotResult(
        ok=True,
        snapshot_index=index,
        snapshot_path=snap_dir,
        project_name=project_name,
        media_files_copied=copied,
        media_files_deduped=deduped,
        description=description,
    )
