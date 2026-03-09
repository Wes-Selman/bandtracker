"""
core/restore.py — Increment 6

bandtracker restore <n>

Safe rollback to any previous snapshot.

Contract:
  - GarageBand must be closed before restore (hard check, not advisory)
  - Both live/ and the original GB bundle are updated atomically
  - On any failure after backups are taken, both files are rolled back
  - A new snapshot is taken after every successful restore so the timeline
    stays append-only (the restore itself becomes part of the history)

Failure modes handled:
  - Target snapshot index does not exist
  - GarageBand process is running
  - GarageBand lock file present inside the bundle
  - Target ProjectData missing (corrupt snapshot)
  - gb_bundle_path missing from project.json or not found on disk
    (restore fails hard — GB would open the wrong version otherwise)
  - Insufficient disk space (checked before any write)
  - Interrupted mid-write (both backups rolled back automatically)
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

_GB_LOCK_FILENAME = ".lck"


def _garageband_process_running() -> bool:
    """Return True if GarageBand appears to be running via pgrep.

    Returns False on non-macOS platforms so tests pass in CI.
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


def _resolve_gb_project_data(project: Project) -> tuple[Optional[Path], Optional[str]]:
    """Resolve the ProjectData path inside the original GarageBand bundle.

    Returns (path, None) on success, or (None, error_message) on failure.

    gb_bundle_path is stored as a ~/... string for cross-machine readability.
    We expand ~ before use and verify the path exists.
    """
    if not project.gb_bundle_path:
        return None, (
            "gb_bundle_path is not set in project.json. "
            "Run `bandtracker set-gb` to register the GarageBand bundle path."
        )

    gb_band = Path(project.gb_bundle_path).expanduser()
    if not gb_band.exists():
        return None, (
            f"GarageBand bundle not found at {gb_band}. "
            "If the file was moved, run `bandtracker set-gb` to update the path."
        )

    gb_pd = gb_band / "Alternatives" / "000" / "ProjectData"
    if not gb_pd.exists():
        return None, (
            f"ProjectData not found inside GarageBand bundle at {gb_pd}. "
            "The bundle may be corrupt."
        )

    return gb_pd, None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def restore(
    provider: StorageProvider,
    project_name: str,
    target_index: int,
    *,
    author: str = "unknown",
    force: bool = False,
    dry_run: bool = False,
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
    # 4. Resolve GarageBand bundle ProjectData path
    #    Fail fast before any writes — no point restoring live/ if
    #    GarageBand won't see the result.
    # ------------------------------------------------------------------
    gb_project_data, gb_err = _resolve_gb_project_data(project)
    if gb_err:
        return RestoreResult(
            success=False,
            restored_snapshot_index=target_index,
            new_snapshot_index=None,
            project_root=project_root,
            errors=[gb_err],
        )

    # ------------------------------------------------------------------
    # 5. Locate live ProjectData
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
    # 6. Disk-space check
    #    Budget: 3× source size — live backup + GB backup + new write
    # ------------------------------------------------------------------
    needed = _file_size(source_project_data)
    budget = needed * 3
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
    # 7. Dry-run exits here
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
    # 8. Atomic replacement of both live/ and GarageBand bundle
    #
    # Strategy:
    #   a) Backup live ProjectData
    #   b) Backup GB ProjectData
    #   c) Write snapshot content → live ProjectData (temp+rename)
    #   d) Write snapshot content → GB ProjectData  (temp+rename)
    #
    # On any failure, _rollback() restores both from their backups.
    # Both writes succeed or neither does.
    # ------------------------------------------------------------------
    live_pd_dir = live_project_data.parent
    live_pd_dir.mkdir(parents=True, exist_ok=True)
    gb_pd_dir = gb_project_data.parent

    # a) Backup live ProjectData
    live_backup_fd, live_backup_str = tempfile.mkstemp(
        dir=live_pd_dir, prefix=".bt_backup_", suffix=".ProjectData"
    )
    os.close(live_backup_fd)
    live_backup = Path(live_backup_str)

    # b) Backup GB ProjectData
    gb_backup_fd, gb_backup_str = tempfile.mkstemp(
        dir=gb_pd_dir, prefix=".bt_backup_", suffix=".ProjectData"
    )
    os.close(gb_backup_fd)
    gb_backup = Path(gb_backup_str)

    def _rollback(reason: str) -> RestoreResult:
        """Restore both live/ and GB ProjectData from their backups."""
        # Restore live/
        try:
            shutil.copy2(live_backup, live_project_data)
        except Exception as rb_err:
            errors.append(
                f"CRITICAL: live/ rollback failed ({rb_err}). "
                f"Backup is at {live_backup}."
            )
        else:
            live_backup.unlink(missing_ok=True)

        # Restore GB bundle
        try:
            shutil.copy2(gb_backup, gb_project_data)
        except Exception as rb_err:
            errors.append(
                f"CRITICAL: GarageBand bundle rollback failed ({rb_err}). "
                f"Backup is at {gb_backup}."
            )
        else:
            gb_backup.unlink(missing_ok=True)

        errors.append(reason)
        return RestoreResult(
            success=False,
            restored_snapshot_index=target_index,
            new_snapshot_index=None,
            project_root=project_root,
            errors=errors,
            warnings=warnings,
        )

    # Take backups
    try:
        shutil.copy2(live_project_data, live_backup)
    except Exception as exc:
        live_backup.unlink(missing_ok=True)
        gb_backup.unlink(missing_ok=True)
        return RestoreResult(
            success=False,
            restored_snapshot_index=target_index,
            new_snapshot_index=None,
            project_root=project_root,
            errors=[f"Could not back up live ProjectData: {exc}"],
        )

    try:
        shutil.copy2(gb_project_data, gb_backup)
    except Exception as exc:
        live_backup.unlink(missing_ok=True)
        gb_backup.unlink(missing_ok=True)
        return RestoreResult(
            success=False,
            restored_snapshot_index=target_index,
            new_snapshot_index=None,
            project_root=project_root,
            errors=[f"Could not back up GarageBand ProjectData: {exc}"],
        )

    # c) Write to live/ atomically
    try:
        tmp_fd, tmp_str = tempfile.mkstemp(
            dir=live_pd_dir, prefix=".bt_restore_", suffix=".ProjectData"
        )
        os.close(tmp_fd)
        tmp_path = Path(tmp_str)
        shutil.copy2(source_project_data, tmp_path)
        tmp_path.replace(live_project_data)
    except Exception as exc:
        return _rollback(f"Write to live/ failed: {exc}")

    # d) Write to GB bundle atomically
    try:
        gb_tmp_fd, gb_tmp_str = tempfile.mkstemp(
            dir=gb_pd_dir, prefix=".bt_restore_", suffix=".ProjectData"
        )
        os.close(gb_tmp_fd)
        gb_tmp_path = Path(gb_tmp_str)
        shutil.copy2(source_project_data, gb_tmp_path)
        gb_tmp_path.replace(gb_project_data)
    except Exception as exc:
        return _rollback(f"Write to GarageBand bundle failed: {exc}")

    # ------------------------------------------------------------------
    # 9. Confirmation snapshot
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

    # Clean up backups
    live_backup.unlink(missing_ok=True)
    gb_backup.unlink(missing_ok=True)

    return RestoreResult(
        success=True,
        restored_snapshot_index=target_index,
        new_snapshot_index=new_index,
        project_root=project_root,
        warnings=warnings,
    )
