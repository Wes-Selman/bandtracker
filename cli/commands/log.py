"""
cli/commands/log.py — `bandtracker log`

Usage
-----
  bandtracker log [--reverse] [--project NAME] [--root PATH]

Lists all snapshots in reverse chronological order (newest first).
One line per snapshot: index, description, author, timestamp,
milestone if set.

  --reverse   Show oldest first.
"""

from __future__ import annotations

import argparse
import sys

from core.init import validate_project_name
from core.models import ProjectPaths
from core.project_ops import get_log
from cli.resolver import make_provider, resolve_project


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "log",
        help="List all snapshots.",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--reverse",
        action="store_true",
        default=False,
        help="Show oldest first instead of newest first.",
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

    result = get_log(paths)

    if not result.ok:
        for err in result.errors:
            print(f"Error: {err}", file=sys.stderr)
        return 1

    entries = result.entries
    if args.reverse:
        entries = list(reversed(entries))

    if not entries:
        print("No snapshots yet.")
        return 0

    for entry in entries:
        milestone_str = f"  [{entry.milestone}]" if entry.milestone else ""
        print(
            f"{entry.index:03d}  {entry.description}"
            f"  ({entry.author}, {entry.timestamp}){milestone_str}"
        )

    for warning in result.warnings:
        print(f"Warning: {warning}", file=sys.stderr)

    return 0
