"""
cli/commands/learn_noise.py

`bandtracker learn-noise <project-name>` — build the noise mask for a project.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from core.diff.noise import build_noise_mask, save_noise_mask
from core.models import ProjectPaths, StorageProvider


def add_subparser(subparsers) -> None:
    p = subparsers.add_parser(
        "learn-noise",
        help="Build the noise mask for a project (one-time setup)",
        description=(
            "Diff the latest snapshot against the current live save to "
            "discover which byte offsets GarageBand always rewrites. "
            "Save with NO intentional changes before running this."
        ),
    )
    p.add_argument(
        "project_name",
        metavar="PROJECT",
        help="Name of the project (folder name under projects/)",
    )
    p.add_argument(
        "--root",
        metavar="DIR",
        default=str(Path.home() / "BandTracker"),
        help="BandTracker root folder (default: ~/BandTracker)",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    root_path = Path(args.root).expanduser().resolve()
    provider = StorageProvider.local(root_path)
    project_name = args.project_name

    project_root = provider.project_path(project_name)
    paths = ProjectPaths(project_root)

    if not paths.project_json.exists():
        print(f"error: project '{project_name}' not found at {project_root}",
              file=sys.stderr)
        return 1

    indices = paths.all_snapshot_indices()
    if not indices:
        print("error: no snapshots found — run `bandtracker snapshot` first",
              file=sys.stderr)
        return 1

    latest_index = indices[-1]
    baseline_pd = paths.snapshot_project_data(latest_index)
    live_pd = paths.live_project_data(project_name)

    if not baseline_pd.exists():
        print(f"error: snapshot {latest_index:03d} has no ProjectData at {baseline_pd}",
              file=sys.stderr)
        return 1

    if not live_pd.exists():
        print(f"error: live ProjectData not found at {live_pd}", file=sys.stderr)
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
        "project_name":      project_name,
        "baseline_snapshot": latest_index,
        "baseline_path":     str(baseline_pd),
        "live_path":         str(live_pd),
        "noisy_bytes":       len(noisy_offsets),
    }
    save_noise_mask(paths.noise_mask_json, noisy_offsets, meta)

    print(f"✓ Noise mask saved -> {paths.noise_mask_json}")
    print(f"  {len(noisy_offsets):,} byte positions will be filtered from future diffs.")
    print()
    print("  Future snapshots will produce cleaner diff summaries.")
    print("  Re-run this command after upgrading GarageBand.")

    return 0
