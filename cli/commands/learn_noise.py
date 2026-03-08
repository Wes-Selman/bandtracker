"""
cli/commands/learn_noise.py

`bandtracker learn-noise` — build the noise mask for a project.

GarageBand rewrites ~18,600 byte ranges on every save regardless of
what the user actually changed. This command learns which offsets those
are by diffing two identical saves of the same project, then writes the
result to noise_mask.json in the project root.

This is a one-time setup step per project. Re-run it if you upgrade
GarageBand (the noisy offsets may shift between versions).

Workflow:
    1. Open the live project in GarageBand
    2. Make NO changes
    3. File → Save (⌘S)
    4. bandtracker learn-noise <project-name>

The command reads the current live ProjectData as the "changed" side
and the most recent snapshot's ProjectData as the "baseline" side.
Both represent the same project state — the only differences are
GarageBand's spurious rewrites.
"""

from __future__ import annotations

from pathlib import Path

from core.diff.noise import build_noise_mask, save_noise_mask
from core.models import ProjectPaths, StorageProvider


def run(
    provider: StorageProvider,
    project_name: str,
) -> int:
    """
    Entry point for `bandtracker learn-noise <project-name>`.

    Returns exit code: 0 on success, 1 on failure.
    """
    project_root = provider.project_path(project_name)
    paths = ProjectPaths(project_root)

    if not paths.project_json.exists():
        print(f"error: project '{project_name}' not found at {project_root}")
        return 1

    # Find the most recent snapshot to use as the baseline
    indices = paths.all_snapshot_indices()
    if not indices:
        print("error: no snapshots found — run `bandtracker snapshot` first")
        return 1

    latest_index = indices[-1]
    baseline_pd = paths.snapshot_project_data(latest_index)
    live_pd = paths.live_project_data(project_name)

    if not baseline_pd.exists():
        print(f"error: snapshot {latest_index:03d} has no ProjectData at {baseline_pd}")
        return 1

    if not live_pd.exists():
        print(f"error: live ProjectData not found at {live_pd}")
        return 1

    print(f"Learning noise mask for '{project_name}'...")
    print(f"  Baseline:  snapshot {latest_index:03d}")
    print(f"  Live file: {live_pd}")
    print()
    print("  Make sure you saved GarageBand with NO intentional changes.")
    print("  Any real edits will pollute the noise mask.\n")

    baseline_bytes = baseline_pd.read_bytes()
    live_bytes = live_pd.read_bytes()

    noisy_offsets = build_noise_mask(baseline_bytes, live_bytes)

    meta = {
        "project_name":    project_name,
        "baseline_snapshot": latest_index,
        "baseline_path":   str(baseline_pd),
        "live_path":       str(live_pd),
        "noisy_bytes":     len(noisy_offsets),
    }
    save_noise_mask(paths.noise_mask_json, noisy_offsets, meta)

    print(f"  ✓ Noise mask saved → {paths.noise_mask_json}")
    print(f"    {len(noisy_offsets):,} byte positions will be filtered from future diffs.")
    print()
    print("  Future snapshots will now produce cleaner diff summaries.")
    print("  Re-run this command after upgrading GarageBand.")

    return 0
