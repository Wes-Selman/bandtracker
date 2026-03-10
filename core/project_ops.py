"""
core/project_ops.py

Project management operations for BandTracker — Increment 9.

Five operations:
  get_status()           current project state at a glance
  get_log()              list all snapshots in reverse chronological order
  add_collaborator()     add a collaborator to project.json
  remove_collaborator()  remove a collaborator from project.json
  rename_project()       rename the project folder and update project.json

Design:
  - Every function returns a typed result dataclass with .ok, .errors, .warnings
  - No exceptions bubble to the CLI
  - No sys.exit, no argparse, no print — that's the CLI layer's job
  - All path logic through ProjectPaths
  - All JSON writes via write_json_atomic()
  - All result dataclass fields are JSON-safe types (str, int, bool, list, dict, None)
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.models import (
    Collaborator,
    Handoff,
    LockState,
    Project,
    ProjectPaths,
    Snapshot,
    StorageProvider,
)
from core.init import validate_project_name, write_json_atomic


# ─────────────────────────────────────────────────────────────
# RESULT TYPES
# ─────────────────────────────────────────────────────────────

@dataclass
class StatusResult:
    """Return value from get_status()."""
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    project_name: Optional[str] = None

    # Latest snapshot info (None if no snapshots yet)
    latest_snapshot_index: Optional[int] = None
    latest_snapshot_description: Optional[str] = None
    latest_snapshot_timestamp: Optional[str] = None   # ISO format
    latest_snapshot_author: Optional[str] = None
    latest_snapshot_milestone: Optional[str] = None

    # Lock state
    lock_state: Optional[str] = None       # "open" or "locked"
    active_editor: Optional[str] = None

    # Unsaved changes
    has_unsaved_changes: bool = False


@dataclass
class LogEntry:
    """One snapshot in the log output."""
    index: int
    description: str
    author: str
    timestamp: str          # ISO format
    milestone: Optional[str] = None


@dataclass
class LogResult:
    """Return value from get_log()."""
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    entries: list[LogEntry] = field(default_factory=list)


@dataclass
class AddCollaboratorResult:
    """Return value from add_collaborator()."""
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    identifier: Optional[str] = None
    display_name: Optional[str] = None


@dataclass
class RemoveCollaboratorResult:
    """Return value from remove_collaborator()."""
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    identifier: Optional[str] = None


@dataclass
class RenameResult:
    """Return value from rename_project()."""
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    old_name: Optional[str] = None
    new_name: Optional[str] = None


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


def _load_handoff(paths: ProjectPaths) -> tuple[Optional[Handoff], str]:
    """Load handoff.json. Returns (handoff, error_message)."""
    if not paths.handoff_json.exists():
        return None, f"handoff.json not found at {paths.handoff_json}"
    try:
        return Handoff.from_json(paths.handoff_json.read_text()), ""
    except Exception as e:
        return None, f"Could not parse handoff.json: {e}"


def _load_snapshot(paths: ProjectPaths, index: int) -> tuple[Optional[Snapshot], str]:
    """Load meta.json for a snapshot. Returns (snapshot, error_message)."""
    meta_path = paths.snapshot_meta(index)
    if not meta_path.exists():
        return None, f"Snapshot {index:03d} meta.json not found at {meta_path}"
    try:
        return Snapshot.from_json(meta_path.read_text()), ""
    except Exception as e:
        return None, f"Could not parse meta.json for snapshot {index:03d}: {e}"


def _sanitize_raw_name(raw: str) -> str:
    """
    Apply the same sanitization rules as sanitize_project_name() from
    core/init.py, but on a raw string instead of a Path stem.

    Rules:
      - Remove characters unsafe in folder names
      - Normalize whitespace
      - Collapse to "Untitled Project" if nothing remains
    """
    name = re.sub(r"[^\w\s\-'()]", "", raw)
    name = re.sub(r"\s+", " ", name).strip()
    if not name:
        name = "Untitled Project"
    return name


# ─────────────────────────────────────────────────────────────
# GET STATUS
# ─────────────────────────────────────────────────────────────

def get_status(
    paths: ProjectPaths,
    project_name: str,
) -> StatusResult:
    """
    Current project state at a glance.

    Reads project.json, handoff.json, latest snapshot meta.json,
    and compares live/ ProjectData against the latest snapshot's
    ProjectData to produce an unsaved-changes indicator.

    Args:
        paths           ProjectPaths for the project
        project_name    name of the project (needed for live_project_data path)

    Returns StatusResult with ok=True on success.
    """
    project, load_err = _load_project(paths)
    if load_err:
        return StatusResult(ok=False, errors=[load_err])

    result = StatusResult(ok=True, project_name=project.name)

    # ── Lock state ─────────────────────────────────────────────
    handoff, handoff_err = _load_handoff(paths)
    if handoff_err:
        result.warnings.append(f"Could not read handoff state: {handoff_err}")
    else:
        result.lock_state = handoff.lock_state.value
        result.active_editor = handoff.active_editor

    # ── Latest snapshot ────────────────────────────────────────
    if project.latest_snapshot is not None:
        snap, snap_err = _load_snapshot(paths, project.latest_snapshot)
        if snap_err:
            result.warnings.append(snap_err)
        else:
            result.latest_snapshot_index = snap.index
            result.latest_snapshot_description = snap.description
            result.latest_snapshot_timestamp = snap.timestamp.isoformat()
            result.latest_snapshot_author = snap.author
            if snap.milestone:
                result.latest_snapshot_milestone = snap.milestone.value

    # ── Unsaved changes ────────────────────────────────────────
    if project.latest_snapshot is not None:
        live_pd = paths.live_project_data(project_name)
        snap_pd = paths.snapshot_project_data(project.latest_snapshot)

        if live_pd.exists() and snap_pd.exists():
            try:
                result.has_unsaved_changes = (
                    live_pd.read_bytes() != snap_pd.read_bytes()
                )
            except OSError:
                result.warnings.append(
                    "Could not compare live and snapshot ProjectData."
                )

    return result


# ─────────────────────────────────────────────────────────────
# GET LOG
# ─────────────────────────────────────────────────────────────

def get_log(paths: ProjectPaths) -> LogResult:
    """
    List all snapshots in reverse chronological order.

    Reads meta.json for every snapshot on disk. Returns entries
    sorted by index descending (newest first).

    Args:
        paths   ProjectPaths for the project

    Returns LogResult with ok=True on success. Individual snapshots
    that fail to load produce warnings, not errors.
    """
    project, load_err = _load_project(paths)
    if load_err:
        return LogResult(ok=False, errors=[load_err])

    indices = paths.all_snapshot_indices()
    entries: list[LogEntry] = []
    warnings: list[str] = []

    for idx in indices:
        snap, snap_err = _load_snapshot(paths, idx)
        if snap_err:
            warnings.append(snap_err)
            continue

        entries.append(LogEntry(
            index=snap.index,
            description=snap.description,
            author=snap.author,
            timestamp=snap.timestamp.isoformat(),
            milestone=snap.milestone.value if snap.milestone else None,
        ))

    # Reverse chronological (newest first)
    entries.sort(key=lambda e: e.index, reverse=True)

    return LogResult(ok=True, warnings=warnings, entries=entries)


# ─────────────────────────────────────────────────────────────
# ADD COLLABORATOR
# ─────────────────────────────────────────────────────────────

def add_collaborator(
    paths: ProjectPaths,
    display_name: str,
    identifier: str,
) -> AddCollaboratorResult:
    """
    Add a collaborator to project.json.

    Args:
        paths           ProjectPaths for the project
        display_name    human-readable name
        identifier      opaque identifier (never parsed)

    Error if identifier already exists.
    """
    project, load_err = _load_project(paths)
    if load_err:
        return AddCollaboratorResult(ok=False, errors=[load_err])

    if project.get_collaborator(identifier) is not None:
        return AddCollaboratorResult(
            ok=False,
            errors=[
                f"Collaborator with identifier '{identifier}' already exists."
            ],
        )

    project.add_collaborator(
        Collaborator(display_name=display_name, identifier=identifier)
    )

    try:
        write_json_atomic(paths.project_json, project.to_json())
    except Exception as e:
        return AddCollaboratorResult(
            ok=False,
            errors=[f"Could not write project.json: {e}"],
        )

    return AddCollaboratorResult(
        ok=True,
        identifier=identifier,
        display_name=display_name,
    )


# ─────────────────────────────────────────────────────────────
# REMOVE COLLABORATOR
# ─────────────────────────────────────────────────────────────

def remove_collaborator(
    paths: ProjectPaths,
    identifier: str,
) -> RemoveCollaboratorResult:
    """
    Remove a collaborator from project.json.

    Must not remove the owner. Warns (not errors) if not found.
    """
    project, load_err = _load_project(paths)
    if load_err:
        return RemoveCollaboratorResult(ok=False, errors=[load_err])

    # Cannot remove the owner
    if identifier == project.owner:
        return RemoveCollaboratorResult(
            ok=False,
            errors=[
                f"Cannot remove '{identifier}' — they are the project owner."
            ],
        )

    # Check if collaborator exists
    existing = project.get_collaborator(identifier)
    if existing is None:
        return RemoveCollaboratorResult(
            ok=True,
            warnings=[
                f"Collaborator '{identifier}' not found in this project."
            ],
            identifier=identifier,
        )

    # Remove
    project.collaborators = [
        c for c in project.collaborators if c.identifier != identifier
    ]

    try:
        write_json_atomic(paths.project_json, project.to_json())
    except Exception as e:
        return RemoveCollaboratorResult(
            ok=False,
            errors=[f"Could not write project.json: {e}"],
        )

    return RemoveCollaboratorResult(
        ok=True,
        identifier=identifier,
    )


# ─────────────────────────────────────────────────────────────
# RENAME PROJECT
# ─────────────────────────────────────────────────────────────

def rename_project(
    provider: StorageProvider,
    project_name: str,
    new_name_raw: str,
) -> RenameResult:
    """
    Rename the project folder on disk and update project.json.

    Args:
        provider        storage provider
        project_name    current project name (folder name)
        new_name_raw    new name as typed by the user (will be sanitized)

    Sanitizes new_name_raw using the same rules as init.
    Errors if the sanitized name collides with an existing project.
    """
    # ── Sanitize ───────────────────────────────────────────────
    new_name = _sanitize_raw_name(new_name_raw)

    # ── Validate ───────────────────────────────────────────────
    try:
        validate_project_name(new_name)
    except ValueError as e:
        return RenameResult(ok=False, errors=[str(e)])

    # ── Same name? ─────────────────────────────────────────────
    if new_name == project_name:
        return RenameResult(
            ok=True,
            warnings=["New name is the same as the current name. No change."],
            old_name=project_name,
            new_name=new_name,
        )

    # ── Check collision ────────────────────────────────────────
    old_path = provider.project_path(project_name)
    new_path = provider.project_path(new_name)

    if not old_path.exists():
        return RenameResult(
            ok=False,
            errors=[f"Project folder not found: {old_path}"],
        )

    if new_path.exists():
        return RenameResult(
            ok=False,
            errors=[
                f"A project named '{new_name}' already exists at {new_path}."
            ],
        )

    # ── Load project.json before rename ────────────────────────
    paths = ProjectPaths(old_path)
    project, load_err = _load_project(paths)
    if load_err:
        return RenameResult(ok=False, errors=[load_err])

    # ── Rename folder on disk ──────────────────────────────────
    try:
        old_path.rename(new_path)
    except OSError as e:
        return RenameResult(
            ok=False,
            errors=[f"Could not rename project folder: {e}"],
        )

    # ── Update project.json ────────────────────────────────────
    new_paths = ProjectPaths(new_path)
    project.name = new_name

    try:
        write_json_atomic(new_paths.project_json, project.to_json())
    except Exception as e:
        # Try to roll back the rename
        try:
            new_path.rename(old_path)
        except OSError:
            pass
        return RenameResult(
            ok=False,
            errors=[f"Could not update project.json after rename: {e}"],
        )

    return RenameResult(
        ok=True,
        old_name=project_name,
        new_name=new_name,
    )
