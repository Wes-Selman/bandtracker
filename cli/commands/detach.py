"""
cli/commands/detach.py — `bandtracker detach <filename>`

Usage
-----
  bandtracker detach <filename> [--snapshot N] [--project NAME] [--root PATH]

  <filename>       Exact filename to detach (not a path).
  --snapshot N     Detach from snapshot N. Defaults to latest if omitted.
  --project NAME   Project name. Inferred if only one project exists.
  --root PATH      BandTracker root (default: ~/BandTracker or
                   BANDTRACKER_ROOT env var).

Only removes the attachment from the specified snapshot. History is never
erased. For project-type files, an earlier snapshot's version (if any)
becomes the most recent going forward.
"""

from __future__ import annotations

import argparse
import sys

from core.init import validate_project_name
from core.models import ProjectPaths
from core.sidecar import do_detach
from cli.resolver import make_provider, resolve_project


def add_subparser(subparsers: argparse._SubParsersAction) -> None:  # noqa: SLF001
    p = subparsers.add_parser(
        "detach",
        help="Remove a sidecar file attachment from a snapshot.",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "filename",
        metavar="FILENAME",
        help="Exact filename to detach.",
    )
    p.add_argument(
        "--snapshot",
        type=int,
        metavar="N",
        default=None,
        help="Detach from snapshot N. Defaults to latest if omitted.",
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
    p.set_defaults(func=cmd_detach)


def cmd_detach(args: argparse.Namespace) -> int:
    """Entry point called by the CLI router."""

    # ── Resolve provider
    provider = make_provider(args.root)

    # ── Resolve project name
    project_name = resolve_project(provider, args.project)
    if project_name is None:
        return 1

    try:
        validate_project_name(project_name)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # ── Resolve paths
    project_root = provider.project_path(project_name)
    paths = ProjectPaths(project_root)

    if not paths.project_json.exists():
        print(
            f"Error: '{project_name}' is not a valid BandTracker project.",
            file=sys.stderr,
        )
        return 1

    # ── Run
    result = do_detach(
        paths=paths,
        filename=args.filename,
        snapshot_index=args.snapshot,
    )

    if not result.ok:
        for err in result.errors:
            print(f"Error: {err}", file=sys.stderr)
        return 1

    for warning in result.warnings:
        print(f"Warning: {warning}")

    print(
        f"Detached '{result.filename}' from snapshot {result.snapshot_index:03d}"
    )
    return 0
