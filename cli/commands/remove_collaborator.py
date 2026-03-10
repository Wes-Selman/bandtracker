"""
cli/commands/remove_collaborator.py — `bandtracker remove-collaborator`

Usage
-----
  bandtracker remove-collaborator --id maya@email.com
                                  [--project NAME] [--root PATH]

Removes a collaborator by identifier.
Warns if not found (not an error). Must not remove the owner.
"""

from __future__ import annotations

import argparse
import sys

from core.init import validate_project_name
from core.models import ProjectPaths
from core.project_ops import remove_collaborator
from cli.resolver import make_provider, resolve_project


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "remove-collaborator",
        help="Remove a collaborator from the project.",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--id",
        required=True,
        dest="identifier",
        metavar="IDENTIFIER",
        help="Identifier of the collaborator to remove.",
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

    if not paths.project_json.exists():
        print(
            f"Error: '{project_name}' is not a valid BandTracker project.",
            file=sys.stderr,
        )
        return 1

    result = remove_collaborator(paths, args.identifier)

    if not result.ok:
        for err in result.errors:
            print(f"Error: {err}", file=sys.stderr)
        return 1

    for warning in result.warnings:
        print(f"Warning: {warning}")

    if not result.warnings:
        print(f"✓ Removed collaborator: {result.identifier}")

    return 0
