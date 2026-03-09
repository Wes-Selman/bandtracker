"""
core/reconcile.py

Reconciliation for BandTracker — Increment 5.

Detects and surfaces offline edits: cases where the musician used
GarageBand while the watcher wasn't running, leaving the GB bundle
ahead of the latest snapshot.

Called in two places:
  1. ProjectWatcher.start() — before the Observer begins, so the watcher
     starts from a clean baseline.
  2. `bandtracker reconcile <project>` — standalone command for the
     musician to run any time they suspect drift.

Design:
  - Compares the actual GarageBand bundle (the file GB saves to) against
    the latest snapshot's ProjectData — not live/ vs snapshot. This
    catches offline edits made while the watcher was completely off.
  - After comparison, live/ is synced from the GB bundle so the watcher
    starts from the correct baseline regardless of the reconcile outcome.
  - All path logic goes through ProjectPaths.
  - prompt_fn and print_fn are injectable (no raw input() here).
  - Snapshot taking delegates entirely to core/snapshot.take_snapshot().
  - Returns ReconcileResult so callers can branch on .ok / .action_taken.
  - The diff pipeline is wrapped in try/except everywhere — a diff failure
    never blocks reconciliation from completing.

Flow:
  1. Load project.json → get latest_snapshot index + gb_bundle_path.
  2. Resolve GB bundle path (alias first, stored path fallback, --gb override).
  3. If no snapshots yet → skip (ReconcileAction.SKIPPED).
  4. Read bytes from GB ProjectData and snapshot ProjectData.
  5. If identical → sync live/ from GB → ReconcileAction.CLEAN.
  6. If differ → run diff engine → display changes.
  7. Prompt "Save a version now? [y/n]"
  8. y → take_snapshot() → sync live/ → ReconcileAction.SNAPSHOTTED.
  9. n → sync live/ → ReconcileAction.DEFERRED.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from core.models import Project, ProjectPaths, StorageProvider
from core.bundle_ref import resolve_gb_bundle
from core.diff.engine import byte_diff, build_description
from core.diff.noise import load_noise_mask
from core.diff.interpreter import interpret_changes
from core.snapshot import take_snapshot, SnapshotResult


# ─────────────────────────────────────────────────────────────
# TYPES
# ─────────────────────────────────────────────────────────────

PromptFn = Callable[[str], str]
PrintFn = Callable[[str], None]


# ─────────────────────────────────────────────────────────────
# RESULT
# ─────────────────────────────────────────────────────────────

class ReconcileAction(str, Enum):
    """What reconciliation actually did."""
    CLEAN       = "clean"        # GB matches latest snapshot — no action needed
    SKIPPED     = "skipped"      # no snapshots exist yet — nothing to compare
    SNAPSHOTTED = "snapshotted"  # user said y — snapshot was taken
    DEFERRED    = "deferred"     # user said n — drift acknowledged, not snapshotted
    ERROR       = "error"        # could not complete reconciliation


@dataclass
class ReconcileResult:
    """
    Return value from reconcile().
    Always check .ok before acting on other fields.

    ok is True for CLEAN, SKIPPED, SNAPSHOTTED, and DEFERRED —
    any of these means the caller can proceed normally.
    ok is False only for ERROR.
    """
    ok: bool
    action: ReconcileAction
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # Populated when drift was detected
    latest_snapshot_index: Optional[int] = None
    diff_summary: list[str] = field(default_factory=list)
    description: Optional[str] = None

    # Populated when action == SNAPSHOTTED
    snapshot_result: Optional[SnapshotResult] = None


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


def _read_bytes_safe(path: Path) -> tuple[Optional[bytes], str]:
    """Read a file's bytes. Returns (bytes, error_message)."""
    if not path.exists():
        return None, f"File not found: {path}"
    try:
        return path.read_bytes(), ""
    except Exception as e:
        return None, f"Could not read {path}: {e}"


def _gb_project_data(gb_band_path: Path) -> Path:
    """Path to ProjectData inside a .band bundle."""
    return gb_band_path / "Alternatives" / "000" / "ProjectData"


def _sync_live_from_gb(
    gb_band_path: Path,
    paths: ProjectPaths,
    project_name: str,
) -> Optional[str]:
    """
    Copy the GB bundle into live/ so the watcher has a fresh baseline.
    Returns an error string on failure, None on success.

    This is a best-effort operation — a failure here is surfaced as a
    warning, not a hard error, because the snapshot was already taken
    (or skipped) and the musician's data is safe in the GB bundle.
    """
    live_band = paths.live_band(project_name)
    try:
        if live_band.exists():
            shutil.rmtree(live_band)
        shutil.copytree(gb_band_path, live_band)
        return None
    except OSError as e:
        return f"Could not sync live/ from GB bundle: {e}"


def _run_diff(
    old_bytes: bytes,
    new_bytes: bytes,
    noise_mask_path: Optional[Path],
) -> tuple[list[str], Optional[str]]:
    """
    Run the full diff pipeline. Returns (summary_lines, description).
    Returns ([], None) silently on any failure — a diff failure never
    blocks the reconciliation flow.
    """
    try:
        noise_mask = (
            load_noise_mask(noise_mask_path)
            if noise_mask_path and noise_mask_path.exists()
            else None
        )

        result = byte_diff(old_bytes, new_bytes, noise_mask=noise_mask)
        if not result.ok:
            return [], None

        interpreted = interpret_changes(result, full_changed_bytes=new_bytes)
        description = build_description(result, interpreted)

        summary = interpreted if interpreted else []
        desc = (
            description
            if (description and description != "no changes detected")
            else None
        )
        return summary, desc

    except Exception:
        return [], None


# ─────────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────────

def reconcile(
    provider: StorageProvider,
    project_name: str,
    author: str,
    gb_band_path: Optional[Path] = None,
    prompt_fn: Optional[PromptFn] = None,
    print_fn: Optional[PrintFn] = None,
) -> ReconcileResult:
    """
    Compare the GarageBand bundle against the latest snapshot and offer
    to snapshot if they differ. Syncs live/ from the GB bundle after
    comparison so the watcher starts from a fresh baseline.

    Args:
        provider        storage provider (knows BandTracker root)
        project_name    name of the managed project folder
        author          identifier of the current user
        gb_band_path    explicit path to the GB bundle — overrides the
                        path stored in project.json. Pass None to use
                        the stored path (normal case).
        prompt_fn       callable(str) -> str for user input
                        defaults to input() if None
        print_fn        callable(str) -> None for output
                        defaults to print() if None

    Returns:
        ReconcileResult — ok=True unless something broke unexpectedly.
        Callers should proceed normally on ok=True regardless of action.
    """
    _prompt = prompt_fn or input
    _print = print_fn or print

    project_root = provider.project_path(project_name)
    paths = ProjectPaths(project_root)

    # ── Load project ───────────────────────────────────────────
    project, load_err = _load_project(paths)
    if load_err:
        return ReconcileResult(
            ok=False,
            action=ReconcileAction.ERROR,
            errors=[load_err],
        )

    # ── Resolve GB bundle path ─────────────────────────────────
    # Explicit --gb override takes precedence over stored path.
    if gb_band_path is not None:
        resolved_gb = gb_band_path
    else:
        resolved_gb, resolve_err = resolve_gb_bundle(
            project.gb_bundle_path,
            project.gb_bundle_alias,
        )
        if resolved_gb is None:
            return ReconcileResult(
                ok=False,
                action=ReconcileAction.ERROR,
                errors=[resolve_err],
            )

    # ── No snapshots yet → nothing to reconcile ───────────────
    if project.latest_snapshot is None:
        return ReconcileResult(ok=True, action=ReconcileAction.SKIPPED)

    latest_index = project.latest_snapshot

    # ── Read GB ProjectData ────────────────────────────────────
    gb_pd_path = _gb_project_data(resolved_gb)
    gb_bytes, gb_err = _read_bytes_safe(gb_pd_path)
    if gb_err:
        return ReconcileResult(
            ok=False,
            action=ReconcileAction.ERROR,
            errors=[f"Cannot read GarageBand ProjectData: {gb_err}"],
        )

    # ── Read snapshot ProjectData ──────────────────────────────
    snap_pd_path = paths.snapshot_project_data(latest_index)
    snap_bytes, snap_err = _read_bytes_safe(snap_pd_path)
    if snap_err:
        # Missing snapshot ProjectData is a warning, not a hard error —
        # the snapshot index exists in project.json but the file is gone.
        # Sync live/ and treat as clean so we don't block the watcher.
        sync_err = _sync_live_from_gb(resolved_gb, paths, project_name)
        warnings = [
            f"Snapshot {latest_index} ProjectData missing — "
            f"cannot compare: {snap_err}"
        ]
        if sync_err:
            warnings.append(sync_err)
        return ReconcileResult(
            ok=True,
            action=ReconcileAction.CLEAN,
            warnings=warnings,
        )

    # ── Compare ────────────────────────────────────────────────
    if gb_bytes == snap_bytes:
        sync_err = _sync_live_from_gb(resolved_gb, paths, project_name)
        result = ReconcileResult(
            ok=True,
            action=ReconcileAction.CLEAN,
            latest_snapshot_index=latest_index,
        )
        if sync_err:
            result.warnings.append(sync_err)
        return result

    # ── Drift detected — run diff ──────────────────────────────
    diff_summary, description = _run_diff(
        old_bytes=snap_bytes,
        new_bytes=gb_bytes,
        noise_mask_path=paths.noise_mask_json,
    )

    # ── Surface the drift ──────────────────────────────────────
    _print("")
    _print("── Unsaved changes detected ─────────────────────────────")
    _print(f"  GarageBand project differs from snapshot {latest_index}.")
    if description:
        _print(f"  Changes: {description}")
    elif diff_summary:
        for line in diff_summary:
            _print(f"  • {line}")
    else:
        _print("  (Changes detected but could not be described in detail.)")
    _print("")

    # ── Prompt ────────────────────────────────────────────────
    try:
        answer = _prompt(
            f"Unsaved changes detected since snapshot {latest_index}. "
            f"Save a version now? [y/n] "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"

    if answer != "y":
        # Sync live/ so watcher has the correct baseline even though
        # we didn't snapshot — the drift is acknowledged, not lost.
        sync_err = _sync_live_from_gb(resolved_gb, paths, project_name)
        _print("Continuing without snapshotting. Changes are not lost —")
        _print("they are still in your GarageBand project file.")
        _print("─" * 55)
        result = ReconcileResult(
            ok=True,
            action=ReconcileAction.DEFERRED,
            latest_snapshot_index=latest_index,
            diff_summary=diff_summary,
            description=description,
        )
        if sync_err:
            result.warnings.append(sync_err)
        return result

    # ── Sync live/ from GB before snapshotting ────────────────
    # take_snapshot() reads from live/, so live/ must reflect the GB
    # bundle's current state before we call it. If sync fails we still
    # attempt the snapshot (live/ may already be close enough) and
    # surface the warning.
    sync_err = _sync_live_from_gb(resolved_gb, paths, project_name)

    # ── Take snapshot ─────────────────────────────────────────
    snap_result = take_snapshot(
        provider=provider,
        project_name=project_name,
        author=author,
        message=description or None,
    )

    if snap_result.ok:
        _print(
            f"✓ Snapshot {snap_result.snapshot_index} saved"
            + (f': "{snap_result.description}"' if snap_result.description else "")
        )
        _print("─" * 55)
        result = ReconcileResult(
            ok=True,
            action=ReconcileAction.SNAPSHOTTED,
            latest_snapshot_index=latest_index,
            diff_summary=diff_summary,
            description=description,
            snapshot_result=snap_result,
        )
        if sync_err:
            result.warnings.append(sync_err)
        return result
    else:
        errors = snap_result.errors
        _print(f"✗ Snapshot failed: {'; '.join(errors)}")
        _print("─" * 55)
        result = ReconcileResult(
            ok=False,
            action=ReconcileAction.ERROR,
            errors=errors,
            latest_snapshot_index=latest_index,
            diff_summary=diff_summary,
            description=description,
            snapshot_result=snap_result,
        )
        if sync_err:
            result.warnings.append(sync_err)
        return result
