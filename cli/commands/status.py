"""
cli/commands/status.py — `bandtracker status`

Usage
-----
  bandtracker status [--project NAME] [--root PATH]

Shows current project state at a glance:
  - Latest snapshot index, description, timestamp, author
  - Lock state and active editor
  - Unsaved changes indicator (binary compare, bool only)
"""

from __future__ import annotations

import argparse
import sys

from core.init import validate_project_name
from core.models import ProjectPaths
from core.project_ops import get_status
from cli.resolver import make_provider, resolve_project


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "status",
        help="Show the current project state.",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    provider = make_provider(args.root)

    project_name = resolve_project(provider, args.project)
    if project_name is None:
        return 1

    try:
        validate_project_name(project_name)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    project_root = provider.project_path(project_name)
    paths = ProjectPaths(project_root)

    result = get_status(paths, project_name)

    if not result.ok:
        for err in result.errors:
            print(f"Error: {err}", file=sys.stderr)
        return 1

    # ── Display ────────────────────────────────────────────────
    print(f"Project: {result.project_name}")

    if result.latest_snapshot_index is not None:
        milestone_str = ""
        if result.latest_snapshot_milestone:
            milestone_str = f"  [{result.latest_snapshot_milestone}]"
        print(f"  Latest snapshot: {result.latest_snapshot_index:03d}{milestone_str}")
        print(f"    {result.latest_snapshot_description}")
        print(f"    by {result.latest_snapshot_author} at {result.latest_snapshot_timestamp}")
    else:
        print("  No snapshots yet.")

    if result.lock_state is not None:
        if result.lock_state == "locked" and result.active_editor:
            print(f"  Lock: locked to {result.active_editor}")
        else:
            print(f"  Lock: {result.lock_state}")

    if result.has_unsaved_changes:
        print("  Unsaved changes: yes")
    else:
        print("  Unsaved changes: no")

    for warning in result.warnings:
        print(f"Warning: {warning}", file=sys.stderr)

    return 0
