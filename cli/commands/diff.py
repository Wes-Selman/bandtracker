"""
cli/commands/diff.py

CLI handler for `bandtracker diff`.

Usage:
    bandtracker diff 3                  # snapshot 3 vs current GB bundle
    bandtracker diff 3 5                # snapshot 3 vs snapshot 5
    bandtracker diff 3 --gb ~/Music/Song.band   # explicit GB bundle path
    bandtracker diff 3 --project MyProject --root ~/Dropbox/BandTracker

Thin CLI layer — all business logic lives in core/diff_ops.py.
This module handles argument parsing, resolution, and output formatting.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from cli.resolver import make_provider, resolve_project
from core.diff_ops import compare


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "diff",
        help="Show what changed between two snapshots or between a snapshot and the current file",
        description=(
            "Compare two ProjectData states and display a human-readable diff.\n\n"
            "  bandtracker diff <n>       — snapshot n vs the current GarageBand bundle\n"
            "  bandtracker diff <n> <m>   — snapshot n vs snapshot m"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "baseline",
        type=int,
        metavar="N",
        help="Baseline snapshot index (older).",
    )
    p.add_argument(
        "compared",
        type=int,
        nargs="?",
        default=None,
        metavar="M",
        help="Compared snapshot index. Omit to compare against the current GB bundle.",
    )
    p.add_argument(
        "--gb",
        dest="gb_band_path",
        metavar="PATH",
        default=None,
        help=(
            "Path to the GarageBand .band bundle. Overrides the path "
            "stored in project.json. Only used when comparing against live."
        ),
    )
    p.add_argument(
        "--project",
        metavar="NAME",
        default=None,
        help="Project name (default: inferred or BANDTRACKER_PROJECT env var).",
    )
    p.add_argument(
        "--root",
        metavar="PATH",
        default=None,
        help="BandTracker root directory (default: ~/BandTracker or BANDTRACKER_ROOT).",
    )
    p.set_defaults(func=cmd_diff)


def cmd_diff(args: argparse.Namespace) -> int:
    """Entry point called by the CLI router."""

    # ── Resolve provider ───────────────────────────────────────
    provider = make_provider(args.root)

    # ── Resolve project name ───────────────────────────────────
    project_name = resolve_project(provider, args.project)
    if project_name is None:
        return 1

    # ── Validate arguments ─────────────────────────────────────
    if args.baseline < 1:
        print(
            f"Error: Baseline snapshot index must be 1 or greater (got {args.baseline}).",
            file=sys.stderr,
        )
        return 1

    if args.compared is not None and args.compared < 1:
        print(
            f"Error: Compared snapshot index must be 1 or greater (got {args.compared}).",
            file=sys.stderr,
        )
        return 1

    gb_override: Optional[Path] = None
    if args.gb_band_path:
        if args.compared is not None:
            print(
                "Error: --gb is only valid when comparing against the live GB bundle, "
                "not when comparing two snapshots.",
                file=sys.stderr,
            )
            return 1
        gb_override = Path(args.gb_band_path).expanduser().resolve()

    # ── Run comparison ─────────────────────────────────────────
    result = compare(
        provider=provider,
        project_name=project_name,
        baseline_index=args.baseline,
        compared_index=args.compared,
        gb_override=gb_override,
    )

    # ── Output ─────────────────────────────────────────────────
    for warning in result.warnings:
        print(f"Warning: {warning}", file=sys.stderr)

    if not result.ok:
        for error in result.errors:
            print(f"Error: {error}", file=sys.stderr)
        return 1

    # Header
    if result.compared_index is not None:
        print(
            f"Comparing snapshot {result.baseline_index:03d} → "
            f"snapshot {result.compared_index:03d}"
        )
    else:
        print(
            f"Comparing snapshot {result.baseline_index:03d} → "
            f"current GarageBand file"
        )

    print()

    # Description (the three-tier summary)
    print(f"  {result.description}")

    # Individual interpreted changes, if any
    if result.diff_summary:
        print()
        for line in result.diff_summary:
            print(f"    • {line}")

    # Stats line when there are changes
    if result.num_ranges > 0:
        parts = [f"{result.num_ranges} changed region{'s' if result.num_ranges != 1 else ''}"]
        if result.size_delta != 0:
            direction = "+" if result.size_delta > 0 else ""
            parts.append(f"{direction}{result.size_delta} bytes")
        if result.noise_filtered:
            parts.append("noise filtered")
        print(f"\n  ({', '.join(parts)})")

    return 0
