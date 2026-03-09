"""
core/restore.py — Increment 6

bandtracker restore <n>

Safe rollback to any previous snapshot.

Contract:
  - GarageBand must be closed before restore (hard check, not advisory)
  - live/ ProjectData is replaced atomically: write-then-rename, never partial
  - On any failure after the backup has been taken, live/ is rolled back
  - A new snapshot is taken after every successful restore so the timeline
    stays append-only (the restore itself becomes part of the history)
  - The new snapshot carries milestone=None and a description that records
    which snapshot was restored, so the event is auditable

Failure modes handled:
  - Target snapshot index does not exist
  - GarageBand process is running
  - GarageBand lock file present inside the bundle
  - Target ProjectData missing (corrupt snapshot)
  - Insufficient disk space (checked before any write)
  - Interrupted mid-write (backup rolled back automatically)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.models import Project, ProjectPaths, Snapshot, StorageProvider
from core.snapshot import take_snapshot


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class RestoreResult:
    """Returned by restore(). success=False means nothing was changed."""

    success: bool
    restored_snapshot_index: int        # the snapshot we rolled back to
    new_snapshot_index: Optional[int]   # the confirmation snapshot taken after restore
    project_root: Path
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        if not self.success:
            return "Restore failed:\n" + "\n".join(f"  • {e}" for e in self.errors)
        lines = [
            f"✓ Restored to snapshot {self.restored_snapshot_index:03d}",
        ]
        if self.new_snapshot_index is not None:
            lines.append(
                f"  New snapshot {self.new_snapshot_index:03d} created "
                "to record the restore event"
            )
        lines.append(f"  Project: {self.project_root}")
        if self.warnings:
            lines += [f"  ⚠ {w}" for w in self.warnings]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# GarageBand open-check helpers
# ---------------------------------------------------------------------------

# GarageBand writes .lck inside the .band bundle while the project is open
_GB_LOCK_FILENAME = ".lck"


def _garageband_process_running() -> bool:
    """Return True if GarageBand appears to be running via pgrep.

    Returns False on non-macOS platforms (where pgrep -x may not exist)
    so that tests pass in CI without GarageBand installed.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-x", "GarageBand"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _garageband_lock_present(live_band_path: Path) -> bool:
    """Return True if GarageBand's lock file exists inside the bundle."""
    return (live_band_path / _GB_LOCK_FILENAME).exists()


# ---------------------------------------------------------------------------
# Disk-space helper
# ---------------------------------------------------------------------------

def _free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def _file_size(path: Path) -> int:
    return path.stat().st_size if path.is_file() else 0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_target_snapshot_description(paths: ProjectPaths, index: int) -> Optional[str]:
    """Return the description field of a snapshot, or None if unreadable."""
    meta_path = paths.snapshot_meta(index)
    if not meta_path.exists():
        return None
    try:
        snap = Snapshot.from_json(meta_path.read_text())
        return snap.description or None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def restore(
    provider: StorageProvider,
    project_name: str,
    target_index: int,
    *,
    author: str = "unknown",
    force: bool = False,     # skip the GB-running check (for tests/CI)
    dry_run: bool = False,   # validate everything but don't write
) -> RestoreResult:
    """Restore *project_name* to the state captured in snapshot *target_index*.

    Parameters
    ----------
    provider:
        StorageProvider that knows where projects live.
    project_name:
        Name of the project folder (sanitized, no path separators).
    target_index:
        1-based snapshot number to restore to.
    author:
        Written into the new confirmation snapshot's meta.json.
    force:
        Skip the GarageBand-running check. Use only in tests or CI.
    dry_run:
        Validate all preconditions and return a result without writing anything.
    """
    errors: list[str] = []
    warnings: list[str] = []

    project_root = provider.project_path(project_name)
    paths = ProjectPaths(project_root)

    # ------------------------------------------------------------------
    # 1. Load project
    # ------------------------------------------------------------------
    if not paths.project_json.exists():
        return RestoreResult(
            success=False,
            restored_snapshot_index=target_index,
            new_snapshot_index=None,
            project_root=project_root,
            errors=[f"project.json not found at {project_root}"],
        )

    try:
        project = Project.from_json(paths.project_json.read_text())
    except Exception as exc:
        return RestoreResult(
            success=False,
            restored_snapshot_index=target_index,
            new_snapshot_index=None,
            project_root=project_root,
            errors=[f"Could not parse project.json: {exc}"],
        )

    # ------------------------------------------------------------------
    # 2. Validate target snapshot exists and has ProjectData
    # ------------------------------------------------------------------
    snapshot_dir = paths.snapshot(target_index)
    if not snapshot_dir.exists():
        return RestoreResult(
            success=False,
            restored_snapshot_index=target_index,
            new_snapshot_index=None,
            project_root=project_root,
            errors=[
                f"Snapshot {target_index:03d} does not exist. "
                f"Latest snapshot is {project.latest_snapshot:03d}."
            ],
        )

    source_project_data = paths.snapshot_project_data(target_index)
    if not source_project_data.exists():
        return RestoreResult(
            success=False,
            restored_snapshot_index=target_index,
            new_snapshot_index=None,
            project_root=project_root,
            errors=[
                f"Snapshot {target_index:03d} is missing its ProjectData file. "
                "The snapshot may be corrupt."
            ],
        )

    # ------------------------------------------------------------------
    # 3. Check GarageBand is closed
    # ------------------------------------------------------------------
    if not force:
        if _garageband_process_running():
            return RestoreResult(
                success=False,
                restored_snapshot_index=target_index,
                new_snapshot_index=None,
                project_root=project_root,
                errors=["GarageBand is open. Close it before restoring."],
            )

        live_band = paths.live_band(project_name)
        if live_band.exists() and _garageband_lock_present(live_band):
            return RestoreResult(
                success=False,
                restored_snapshot_index=target_index,
                new_snapshot_index=None,
                project_root=project_root,
                errors=[
                    "GarageBand lock file found inside the bundle. "
                    "Close GarageBand and try again."
                ],
            )

    # ------------------------------------------------------------------
    # 4. Locate live ProjectData
    # ------------------------------------------------------------------
    live_project_data = paths.live_project_data(project_name)
    if not live_project_data.exists():
        return RestoreResult(
            success=False,
            restored_snapshot_index=target_index,
            new_snapshot_index=None,
            project_root=project_root,
            errors=[f"Live ProjectData not found at {live_project_data}"],
        )

    # ------------------------------------------------------------------
    # 5. Disk-space check
    # ------------------------------------------------------------------
    needed = _file_size(source_project_data)
    # Budget: 2× source size (backup copy + new write)
    budget = needed * 2
    free = _free_bytes(project_root)
    if free < budget:
        return RestoreResult(
            success=False,
            restored_snapshot_index=target_index,
            new_snapshot_index=None,
            project_root=project_root,
            errors=[
                f"Insufficient disk space. "
                f"Need ~{budget // 1024} KB, have {free // 1024} KB free."
            ],
        )

    # ------------------------------------------------------------------
    # 6. Dry-run exits here
    # ------------------------------------------------------------------
    if dry_run:
        return RestoreResult(
            success=True,
            restored_snapshot_index=target_index,
            new_snapshot_index=None,
            project_root=project_root,
            warnings=["Dry run — no changes written."],
        )

    # ------------------------------------------------------------------
    # 7. Atomic replacement of live ProjectData
    #
    # Strategy:
    #   a) Copy current live → temp backup (same directory = same filesystem)
    #   b) Copy source snapshot → temp file in the same directory
    #   c) Path.replace() temp → live ProjectData  (atomic on POSIX)
    #
    # On any failure, the backup is renamed back over live ProjectData.
    # ------------------------------------------------------------------
    live_pd_dir = live_project_data.parent
    live_pd_dir.mkdir(parents=True, exist_ok=True)

    # a) Backup current live ProjectData
    backup_fd, backup_path_str = tempfile.mkstemp(
        dir=live_pd_dir, prefix=".bt_backup_", suffix=".ProjectData"
    )
    os.close(backup_fd)
    backup_path = Path(backup_path_str)

    def _rollback(reason: str) -> RestoreResult:
        """Attempt to restore live ProjectData from backup."""
        try:
            shutil.copy2(backup_path, live_project_data)
        except Exception as rb_err:
            errors.append(
                f"CRITICAL: rollback also failed ({rb_err}). "
                f"Backup is at {backup_path}. "
                "Manually copy it to restore your project."
            )
        else:
            backup_path.unlink(missing_ok=True)
        errors.append(reason)
        return RestoreResult(
            success=False,
            restored_snapshot_index=target_index,
            new_snapshot_index=None,
            project_root=project_root,
            errors=errors,
            warnings=warnings,
        )

    try:
        shutil.copy2(live_project_data, backup_path)
    except Exception as exc:
        backup_path.unlink(missing_ok=True)
        return RestoreResult(
            success=False,
            restored_snapshot_index=target_index,
            new_snapshot_index=None,
            project_root=project_root,
            errors=[f"Could not back up live ProjectData: {exc}"],
        )

    # b+c) Write new content atomically
    try:
        tmp_fd, tmp_path_str = tempfile.mkstemp(
            dir=live_pd_dir, prefix=".bt_restore_", suffix=".ProjectData"
        )
        os.close(tmp_fd)
        tmp_path = Path(tmp_path_str)

        shutil.copy2(source_project_data, tmp_path)
        tmp_path.replace(live_project_data)  # atomic on POSIX

    except Exception as exc:
        return _rollback(f"Write failed during restore: {exc}")

    # ------------------------------------------------------------------
    # 8. Confirmation snapshot
    #    Records the restore event so the timeline stays append-only.
    # ------------------------------------------------------------------
    target_desc = _load_target_snapshot_description(paths, target_index)
    restore_message = f"Restored to snapshot {target_index:03d}"
    if target_desc:
        restore_message += f' ("{target_desc}")'

    new_index: Optional[int] = None
    try:
        snap_result = take_snapshot(
            provider=provider,
            project_name=project_name,
            author=author,
            message=restore_message,
            milestone=None,
        )
        if snap_result.ok:
            new_index = snap_result.snapshot_index
        else:
            warnings.append(
                f"Restore succeeded but confirmation snapshot failed: "
                f"{'; '.join(snap_result.errors)}. "
                "Run `bandtracker snapshot` manually to record the restore event."
            )
    except Exception as exc:
        warnings.append(
            f"Restore succeeded but confirmation snapshot could not be created: {exc}. "
            "Run `bandtracker snapshot` manually to record the restore event."
        )

    # Clean up backup
    backup_path.unlink(missing_ok=True)

    return RestoreResult(
        success=True,
        restored_snapshot_index=target_index,
        new_snapshot_index=new_index,
        project_root=project_root,
        warnings=warnings,
    )
