"""
core/diff_ops.py

Public-facing diff operations for BandTracker — Increment 10.

Consolidates the diff pipeline (byte_diff → interpret_changes →
build_description) into a single compare() function with two modes:

  1. Snapshot vs GB bundle:  compare(provider, project_name, n)
     Reads snapshot n's ProjectData and the current GB bundle's
     ProjectData. Requires bundle resolution (same as reconcile/watch).

  2. Snapshot vs snapshot:   compare(provider, project_name, n, m)
     Reads both snapshots' ProjectData from disk. No bundle resolution.

Design:
  - Returns CompareResult — typed dataclass with .ok, .errors, .warnings.
  - All path logic through ProjectPaths.
  - Noise mask loaded when available, skipped silently when not.
  - Diff pipeline failures are caught and surfaced in .errors,
    never raised as exceptions.
  - No I/O assumptions beyond the filesystem (no argparse, no sys.exit).
  - All fields are JSON-safe for Increment 12 serialization.

Does NOT:
  - Refactor snapshot.py, reconcile.py, or watcher.py callers.
    Those continue to use their own inline pipelines. A future
    increment may point them at compare() once it's battle-tested.
  - Take snapshots or write any state. This is a read-only operation.
  - Resolve CLI arguments. That's cli/commands/diff.py's job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.models import Project, ProjectPaths, StorageProvider
from core.bundle_ref import resolve_gb_bundle
from core.diff.engine import byte_diff, build_description, DiffResult
from core.diff.noise import load_noise_mask
from core.diff.interpreter import interpret_changes


# ─────────────────────────────────────────────────────────────
# RESULT
# ─────────────────────────────────────────────────────────────

@dataclass
class CompareResult:
    """
    Result of comparing two ProjectData blobs.

    ok              True if the comparison completed successfully
    errors          hard errors that prevented comparison
    warnings        non-fatal issues (e.g. noise mask missing)
    description     single human-readable description string
                    (three-tier: interpreted → structural → identical)
    diff_summary    list of interpreted change descriptions
                    e.g. ["tempo changed to 128 BPM", "track added"]
    baseline_index  snapshot index used as the baseline (older)
    compared_index  snapshot index used as the comparand, or None
                    if comparing against the live GB bundle
    num_ranges      number of changed byte ranges after noise filtering
    size_delta      byte count difference (positive = grew)
    noise_filtered  True if a noise mask was applied
    """
    ok: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    description: str = ""
    diff_summary: list[str] = field(default_factory=list)
    baseline_index: int = 0
    compared_index: Optional[int] = None
    num_ranges: int = 0
    size_delta: int = 0
    noise_filtered: bool = False


# ─────────────────────────────────────────────────────────────
# INTERNAL HELPERS
# ─────────────────────────────────────────────────────────────

def _load_snapshot_project_data(
    paths: ProjectPaths,
    index: int,
) -> tuple[Optional[bytes], Optional[str]]:
    """
    Read ProjectData bytes for a given snapshot index.
    Returns (bytes, None) on success or (None, error_string) on failure.
    """
    pd_path = paths.snapshot_project_data(index)
    if not pd_path.exists():
        return None, f"Snapshot {index:03d} has no ProjectData at {pd_path}"
    try:
        return pd_path.read_bytes(), None
    except OSError as e:
        return None, f"Could not read snapshot {index:03d} ProjectData: {e}"


def _load_gb_project_data(
    project: Project,
    gb_override: Optional[Path],
) -> tuple[Optional[bytes], Optional[str]]:
    """
    Read ProjectData bytes from the live GB bundle.
    Returns (bytes, None) on success or (None, error_string) on failure.
    """
    if gb_override:
        gb_band_path = gb_override
    else:
        gb_band_path, resolve_err = resolve_gb_bundle(
            project.gb_bundle_path,
            project.gb_bundle_alias,
        )
        if gb_band_path is None:
            return None, (
                f"Could not resolve GarageBand bundle: {resolve_err}\n"
                "Pass --gb to specify the bundle path, or run "
                "`bandtracker set-gb` to store it."
            )

    pd_path = gb_band_path / "Alternatives" / "000" / "ProjectData"
    if not pd_path.exists():
        return None, f"ProjectData not found in GB bundle at {pd_path}"
    try:
        return pd_path.read_bytes(), None
    except OSError as e:
        return None, f"Could not read GB bundle ProjectData: {e}"


def _run_pipeline(
    baseline: bytes,
    changed: bytes,
    noise_mask_path: Optional[Path],
) -> tuple[DiffResult, list[str], list[str]]:
    """
    Run byte_diff → interpret_changes, returning
    (diff_result, interpreted_descriptions, warnings).
    """
    warnings: list[str] = []

    noise_mask = None
    if noise_mask_path and noise_mask_path.exists():
        noise_mask = load_noise_mask(noise_mask_path)
        if not noise_mask:
            noise_mask = None
    elif noise_mask_path and not noise_mask_path.exists():
        warnings.append(
            "Noise mask not found — diff may include spurious GarageBand save noise. "
            "Run `bandtracker learn-noise` to build one."
        )

    diff_result = byte_diff(baseline, changed, noise_mask=noise_mask)

    if not diff_result.ok:
        return diff_result, [], warnings

    interpreted = interpret_changes(diff_result, full_changed_bytes=changed)
    return diff_result, interpreted, warnings


# ─────────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────────

def compare(
    provider: StorageProvider,
    project_name: str,
    baseline_index: int,
    compared_index: Optional[int] = None,
    gb_override: Optional[Path] = None,
) -> CompareResult:
    """
    Compare two ProjectData blobs and return a human-readable diff.

    Args:
        provider          storage provider (knows BandTracker root)
        project_name      name of the managed project folder
        baseline_index    snapshot index to use as the baseline (older)
        compared_index    snapshot index to compare against, or None
                          to compare against the live GB bundle
        gb_override       explicit path to the GB bundle — overrides
                          the path stored in project.json

    Returns:
        CompareResult with description, diff summary, and metadata.
    """
    project_root = provider.project_path(project_name)
    paths = ProjectPaths(project_root)

    # ── Load project.json ──────────────────────────────────────
    if not paths.project_json.exists():
        return CompareResult(
            errors=[f"project.json not found at {paths.project_json}"],
        )

    try:
        project = Project.from_json(paths.project_json.read_text())
    except Exception as e:
        return CompareResult(
            errors=[f"Could not parse project.json: {e}"],
        )

    # ── Validate snapshot indices ──────────────────────────────
    available = paths.all_snapshot_indices()

    if baseline_index not in available:
        return CompareResult(
            errors=[f"Snapshot {baseline_index:03d} does not exist."],
        )

    if compared_index is not None and compared_index not in available:
        return CompareResult(
            errors=[f"Snapshot {compared_index:03d} does not exist."],
        )

    if compared_index is not None and baseline_index == compared_index:
        return CompareResult(
            ok=True,
            description="no changes detected",
            baseline_index=baseline_index,
            compared_index=compared_index,
        )

    # ── Load baseline bytes ────────────────────────────────────
    baseline_bytes, err = _load_snapshot_project_data(paths, baseline_index)
    if err:
        return CompareResult(errors=[err])

    # ── Load compared bytes ────────────────────────────────────
    if compared_index is not None:
        changed_bytes, err = _load_snapshot_project_data(paths, compared_index)
    else:
        changed_bytes, err = _load_gb_project_data(project, gb_override)

    if err:
        return CompareResult(errors=[err])

    # ── Run diff pipeline ──────────────────────────────────────
    try:
        diff_result, interpreted, warnings = _run_pipeline(
            baseline_bytes,
            changed_bytes,
            paths.noise_mask_json,
        )
    except Exception as e:
        return CompareResult(
            errors=[f"Diff engine error: {e}"],
        )

    if not diff_result.ok:
        return CompareResult(
            errors=[f"Diff failed: {diff_result.error}"],
            warnings=warnings,
        )

    # ── Build description ──────────────────────────────────────
    description = build_description(diff_result, interpreted)

    return CompareResult(
        ok=True,
        warnings=warnings,
        description=description,
        diff_summary=interpreted,
        baseline_index=baseline_index,
        compared_index=compared_index,
        num_ranges=diff_result.num_changed_ranges,
        size_delta=diff_result.size_delta,
        noise_filtered=diff_result.noise_filtered,
    )
