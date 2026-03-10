"""
cli/commands/rename.py — `bandtracker rename <new-name>`

Usage
-----
  bandtracker rename "New Song Title" [--project NAME] [--root PATH]

Renames the project folder on disk and updates the name in project.json.
The new name is sanitized using the same rules as `init`.
Error if a project with that name already exists in the root.
"""

from __future__ import annotations

import argparse
import sys

from core.init import validate_project_name
from core.project_ops import rename_project
from cli.resolver import make_provider, resolve_project


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "rename",
        help="Rename the project.",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "new_name",
        metavar="NEW_NAME",
        help="New project name (will be sanitized).",
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

    result = rename_project(provider, project_name, args.new_name)

    if not result.ok:
        for err in result.errors:
            print(f"Error: {err}", file=sys.stderr)
        return 1

    for warning in result.warnings:
        print(f"Warning: {warning}")

    if result.old_name != result.new_name:
        print(f"✓ Renamed '{result.old_name}' → '{result.new_name}'")
        if result.gb_bundle_path:
            print(f"  GarageBand bundle: {result.gb_bundle_path}")

    return 0
