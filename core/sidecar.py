"""
core/sidecar.py

Sidecar document management for BandTracker — Increment 8.

Provides attach, detach, and list operations for files associated
with snapshots. Two attachment types are supported:

  VERSION  — pinned to one snapshot. No inheritance. A reference
             recording, a mix bounce for a specific version.

  PROJECT  — living document. The most recent attachment of the same
             filename wins across all snapshots ≤ N (shadowing). A
             lyrics file, a chord chart, ongoing producer notes.

Public API:
    do_attach(paths, project_name, snapshot_index, src_path, sidecar_type)
        → AttachResult

    do_detach(paths, project_name, snapshot_index, filename)
        → DetachResult

    list_attachments(paths, snapshot_index, all_snapshots)
        → ListAttachmentsResult

Result dataclasses all follow the .ok / .errors / .warnings convention.
No exceptions bubble out of this module.
No I/O assumptions outside ProjectPaths.
All JSON writes go through write_json_atomic().
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.models import (
    Project,
    ProjectPaths,
    Snapshot,
    SidecarEntry,
    SidecarType,
)
from core.init import write_json_atomic


# ─────────────────────────────────────────────────────────────
# RESULT DATACLASSES
# ─────────────────────────────────────────────────────────────

@dataclass
class AttachResult:
    """
    Return value from do_attach().
    ok=True even when a file was overwritten (that's a warning, not an error).
    """
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # Populated on success
    snapshot_index: Optional[int] = None
    filename: Optional[str] = None
    sidecar_type: Optional[SidecarType] = None
    overwritten: bool = False


@dataclass
class DetachResult:
    """Return value from do_detach()."""
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # Populated on success
    snapshot_index: Optional[int] = None
    filename: Optional[str] = None


@dataclass
class SidecarItem:
    """
    One attachment as returned by list_attachments().

    When listing with inheritance (no --all), effective_snapshot_index
    equals snapshot_index (the snapshot that owns the winning copy).
    When listing with --all, effective_snapshot_index is always equal
    to snapshot_index.
    """
    filename: str
    sidecar_type: SidecarType
    snapshot_index: int
    size_bytes: int

    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "type": self.sidecar_type.value,
            "snapshot_index": self.snapshot_index,
            "size_bytes": self.size_bytes,
        }


@dataclass
class ListAttachmentsResult:
    """Return value from list_attachments()."""
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    items: list[SidecarItem] = field(default_factory=list)

    # Context for display
    resolved_at_index: Optional[int] = None  # None when all_snapshots=True
    all_snapshots: bool = False


# ─────────────────────────────────────────────────────────────
# INTERNAL HELPERS
# ─────────────────────────────────────────────────────────────

def _load_snapshot(paths: ProjectPaths, index: int) -> tuple[Optional[Snapshot], str]:
    """
    Load meta.json for a snapshot index.
    Returns (snapshot, "") on success, (None, error_message) on failure.
    """
    meta_path = paths.snapshot_meta(index)
    if not meta_path.exists():
        return None, f"Snapshot {index:03d} not found (no meta.json at {meta_path})"
    try:
        return Snapshot.from_json(meta_path.read_text()), ""
    except Exception as e:
        return None, f"Could not parse meta.json for snapshot {index:03d}: {e}"


def _save_snapshot(paths: ProjectPaths, snap: Snapshot) -> Optional[str]:
    """
    Persist a Snapshot's meta.json atomically.
    Returns None on success, an error string on failure.
    """
    try:
        write_json_atomic(paths.snapshot_meta(snap.index), snap.to_json())
        return None
    except Exception as e:
        return f"Could not write meta.json for snapshot {snap.index:03d}: {e}"


def _resolve_snapshot_index(
    paths: ProjectPaths,
    snapshot_index: Optional[int],
) -> tuple[Optional[int], str]:
    """
    Resolve the target snapshot index.
    If snapshot_index is None, returns the latest snapshot index.
    Returns (index, "") on success, (None, error_message) on failure.
    """
    if snapshot_index is not None:
        return snapshot_index, ""

    # Load project.json to find latest
    if not paths.project_json.exists():
        return None, f"project.json not found at {paths.project_json}"
    try:
        project = Project.from_json(paths.project_json.read_text())
    except Exception as e:
        return None, f"Could not parse project.json: {e}"

    if project.latest_snapshot is None:
        return None, "Project has no snapshots yet."

    return project.latest_snapshot, ""


def _file_size(path: Path) -> int:
    """Return file size in bytes, 0 if file doesn't exist or can't be read."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


# ─────────────────────────────────────────────────────────────
# INHERITANCE RESOLUTION
# ─────────────────────────────────────────────────────────────

def resolve_attachments_at(
    paths: ProjectPaths,
    at_index: int,
) -> list[SidecarItem]:
    """
    Compute the effective set of attachments visible at snapshot at_index,
    applying inheritance rules:

      VERSION entries: only those directly on snapshot at_index.
      PROJECT entries: for each unique filename across all snapshots
                       ≤ at_index, the most recent wins (shadowing).

    Returns a list of SidecarItem sorted by (type, filename).
    Silently skips snapshots whose meta.json can't be loaded.
    Missing sidecar files are included with size_bytes=0 (attachment
    record exists but file was deleted externally — not a hard error).
    """
    all_indices = [i for i in paths.all_snapshot_indices() if i <= at_index]

    # Collect VERSION entries only from at_index
    version_items: list[SidecarItem] = []
    snap_at, _ = _load_snapshot(paths, at_index)
    if snap_at:
        for entry in snap_at.sidecar_files:
            if entry.type == SidecarType.VERSION:
                file_path = paths.snapshot_sidecar_file(at_index, entry.filename)
                version_items.append(SidecarItem(
                    filename=entry.filename,
                    sidecar_type=SidecarType.VERSION,
                    snapshot_index=at_index,
                    size_bytes=_file_size(file_path),
                ))

    # Collect PROJECT entries — most recent per filename wins
    # Walk all snapshots ≤ at_index in ascending order; last write wins.
    project_latest: dict[str, tuple[int, SidecarItem]] = {}
    for idx in all_indices:
        snap, _ = _load_snapshot(paths, idx)
        if snap is None:
            continue
        for entry in snap.sidecar_files:
            if entry.type == SidecarType.PROJECT:
                file_path = paths.snapshot_sidecar_file(idx, entry.filename)
                item = SidecarItem(
                    filename=entry.filename,
                    sidecar_type=SidecarType.PROJECT,
                    snapshot_index=idx,
                    size_bytes=_file_size(file_path),
                )
                # Ascending order means later idx always overwrites
                project_latest[entry.filename] = (idx, item)

    project_items = [item for _, item in project_latest.values()]

    combined = version_items + project_items
    combined.sort(key=lambda x: (x.sidecar_type.value, x.filename))
    return combined


def all_attachments_flat(paths: ProjectPaths) -> list[SidecarItem]:
    """
    Return every attachment across every snapshot, flat list sorted by
    (snapshot_index, type, filename). Used for --all output.
    """
    items: list[SidecarItem] = []
    for idx in paths.all_snapshot_indices():
        snap, _ = _load_snapshot(paths, idx)
        if snap is None:
            continue
        for entry in snap.sidecar_files:
            file_path = paths.snapshot_sidecar_file(idx, entry.filename)
            items.append(SidecarItem(
                filename=entry.filename,
                sidecar_type=entry.type,
                snapshot_index=idx,
                size_bytes=_file_size(file_path),
            ))
    items.sort(key=lambda x: (x.snapshot_index, x.sidecar_type.value, x.filename))
    return items


# ─────────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────────

def do_attach(
    paths: ProjectPaths,
    src_path: Path,
    sidecar_type: SidecarType,
    snapshot_index: Optional[int] = None,
) -> AttachResult:
    """
    Attach a file to a snapshot.

    Copies src_path into snapshots/{n}/sidecar/ and records the entry
    in that snapshot's meta.json.

    Args:
        paths           ProjectPaths for the project
        src_path        path to the file to attach (read from here)
        sidecar_type    VERSION or PROJECT
        snapshot_index  which snapshot to attach to; defaults to latest

    Returns AttachResult with ok=True on success.
    If a file with the same name and same type is already attached to the
    target snapshot, it is overwritten and result.overwritten is set to
    True (warning, not error).
    If the same filename exists with a different type, the attach is
    rejected with an error — same filename + different type is treated
    as a conflict, not an overwrite.
    """
    # ── Resolve source file
    if not src_path.exists():
        return AttachResult(ok=False, errors=[f"File not found: {src_path}"])
    if not src_path.is_file():
        return AttachResult(ok=False, errors=[f"Not a file: {src_path}"])

    filename = src_path.name

    # ── Resolve snapshot index
    resolved_index, err = _resolve_snapshot_index(paths, snapshot_index)
    if err:
        return AttachResult(ok=False, errors=[err])

    # ── Load snapshot
    snap, err = _load_snapshot(paths, resolved_index)
    if err:
        return AttachResult(ok=False, errors=[err])

    # ── Ensure sidecar directory exists
    sidecar_dir = paths.snapshot_sidecar(resolved_index)
    try:
        sidecar_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return AttachResult(ok=False, errors=[f"Could not create sidecar directory: {e}"])

    # ── Check for existing entry on this snapshot
    overwritten = False
    existing_entry = next(
        (e for e in snap.sidecar_files if e.filename == filename),
        None,
    )
    if existing_entry is not None:
        if existing_entry.type != sidecar_type:
            return AttachResult(
                ok=False,
                errors=[
                    f"'{filename}' is already attached to snapshot "
                    f"{resolved_index:03d} as type '{existing_entry.type.value}'. "
                    f"Detach it first before re-attaching as "
                    f"'{sidecar_type.value}'."
                ],
            )
        overwritten = True

    # ── Copy file into sidecar/
    dest_path = paths.snapshot_sidecar_file(resolved_index, filename)
    try:
        shutil.copy2(src_path, dest_path)
    except OSError as e:
        return AttachResult(ok=False, errors=[f"Could not copy file: {e}"])

    # ── Update meta.json
    # Replace existing entry if present, otherwise append
    new_entry = SidecarEntry(filename=filename, type=sidecar_type)
    if existing_entry is not None:
        snap.sidecar_files = [
            new_entry if e.filename == filename else e
            for e in snap.sidecar_files
        ]
    else:
        snap.sidecar_files.append(new_entry)

    save_err = _save_snapshot(paths, snap)
    if save_err:
        # Best-effort cleanup of the copied file
        dest_path.unlink(missing_ok=True)
        return AttachResult(ok=False, errors=[save_err])

    result = AttachResult(
        ok=True,
        snapshot_index=resolved_index,
        filename=filename,
        sidecar_type=sidecar_type,
        overwritten=overwritten,
    )
    if overwritten:
        result.warnings.append(
            f"'{filename}' was already attached to snapshot {resolved_index:03d} "
            f"— overwritten."
        )
    return result


def do_detach(
    paths: ProjectPaths,
    filename: str,
    snapshot_index: Optional[int] = None,
) -> DetachResult:
    """
    Remove a file attachment from a specific snapshot.

    Deletes the file from snapshots/{n}/sidecar/ and removes the entry
    from that snapshot's meta.json. Only affects the specified snapshot —
    history is never erased. For PROJECT-type files, detaching from
    snapshot N means an earlier snapshot's version (if any) becomes the
    most recent going forward.

    Args:
        paths           ProjectPaths for the project
        filename        exact filename to detach (not a path)
        snapshot_index  which snapshot to detach from; defaults to latest
    """
    # ── Resolve snapshot index
    resolved_index, err = _resolve_snapshot_index(paths, snapshot_index)
    if err:
        return DetachResult(ok=False, errors=[err])

    # ── Load snapshot
    snap, err = _load_snapshot(paths, resolved_index)
    if err:
        return DetachResult(ok=False, errors=[err])

    # ── Verify entry exists
    entry = next(
        (e for e in snap.sidecar_files if e.filename == filename),
        None,
    )
    if entry is None:
        return DetachResult(
            ok=False,
            errors=[
                f"'{filename}' is not attached to snapshot {resolved_index:03d}."
            ],
        )

    # ── Remove file from disk (best-effort — missing file is not fatal)
    file_path = paths.snapshot_sidecar_file(resolved_index, filename)
    warnings: list[str] = []
    if file_path.exists():
        try:
            file_path.unlink()
        except OSError as e:
            warnings.append(
                f"Could not delete file from disk: {e}. "
                f"Entry removed from meta.json regardless."
            )
    else:
        warnings.append(
            f"File '{filename}' was not found in sidecar directory "
            f"(already deleted?). Entry removed from meta.json."
        )

    # ── Update meta.json
    snap.sidecar_files = [e for e in snap.sidecar_files if e.filename != filename]
    save_err = _save_snapshot(paths, snap)
    if save_err:
        return DetachResult(ok=False, errors=[save_err])

    return DetachResult(
        ok=True,
        warnings=warnings,
        snapshot_index=resolved_index,
        filename=filename,
    )


def list_attachments(
    paths: ProjectPaths,
    snapshot_index: Optional[int] = None,
    all_snapshots: bool = False,
) -> ListAttachmentsResult:
    """
    List attachments for a project.

    Modes:
      all_snapshots=False, snapshot_index=None
          Resolved set at the latest snapshot (with inheritance).
      all_snapshots=False, snapshot_index=N
          Resolved set at snapshot N (with inheritance).
      all_snapshots=True
          Every attachment across every snapshot, flat list.
          snapshot_index is ignored when all_snapshots=True.

    Returns ListAttachmentsResult with items populated.
    """
    if all_snapshots:
        items = all_attachments_flat(paths)
        return ListAttachmentsResult(
            ok=True,
            items=items,
            all_snapshots=True,
        )

    # ── Resolve target index
    resolved_index, err = _resolve_snapshot_index(paths, snapshot_index)
    if err:
        return ListAttachmentsResult(ok=False, errors=[err])

    # ── Verify snapshot exists
    if not paths.snapshot_meta(resolved_index).exists():
        return ListAttachmentsResult(
            ok=False,
            errors=[f"Snapshot {resolved_index:03d} not found."],
        )

    items = resolve_attachments_at(paths, resolved_index)
    return ListAttachmentsResult(
        ok=True,
        items=items,
        resolved_at_index=resolved_index,
        all_snapshots=False,
    )
