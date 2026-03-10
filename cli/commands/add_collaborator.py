"""
cli/commands/add_collaborator.py — `bandtracker add-collaborator`

Usage
-----
  bandtracker add-collaborator --name "Maya" --id maya@email.com
                               [--project NAME] [--root PATH]

Adds a collaborator entry to project.json.
Error if identifier already exists.
"""

from __future__ import annotations

import argparse
import sys

from core.init import validate_project_name
from core.models import ProjectPaths
from core.project_ops import add_collaborator
from cli.resolver import make_provider, resolve_project


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "add-collaborator",
        help="Add a collaborator to the project.",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--name",
        required=True,
        metavar="DISPLAY_NAME",
        help="Display name for the collaborator.",
    )
    p.add_argument(
        "--id",
        required=True,
        dest="identifier",
        metavar="IDENTIFIER",
        help="Unique identifier for the collaborator (e.g. email).",
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

    result = add_collaborator(paths, args.name, args.identifier)

    if not result.ok:
        for err in result.errors:
            print(f"Error: {err}", file=sys.stderr)
        return 1

    print(f"✓ Added collaborator: {result.display_name} ({result.identifier})")

    for warning in result.warnings:
        print(f"Warning: {warning}", file=sys.stderr)

    return 0
