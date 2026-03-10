"""
cli/commands/attach.py — `bandtracker attach <file> --type <project|version>`

Usage
-----
  bandtracker attach <file> --type <project|version> [--snapshot N]
                            [--project NAME] [--root PATH]

  <file>           Path to the file to attach.
  --type           Required. 'version' (snapshot-pinned) or 'project'
                   (inherits forward). No default.
  --snapshot N     Attach to snapshot N. Defaults to latest if omitted.
  --project NAME   Project name. Inferred if only one project exists,
                   otherwise required or resolved via BANDTRACKER_PROJECT.
  --root PATH      BandTracker root (default: ~/BandTracker or
                   BANDTRACKER_ROOT env var).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from core.init import validate_project_name
from core.models import ProjectPaths, SidecarType
from core.sidecar import do_attach
from cli.resolver import make_provider, resolve_project


def add_subparser(subparsers: argparse._SubParsersAction) -> None:  # noqa: SLF001
    p = subparsers.add_parser(
        "attach",
        help="Attach a sidecar file to a snapshot.",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "file",
        metavar="FILE",
        help="Path to the file to attach.",
    )
    p.add_argument(
        "--type",
        dest="sidecar_type",
        required=True,
        choices=["version", "project"],
        metavar="TYPE",
        help="'version' (snapshot-pinned) or 'project' (inherits forward). Required.",
    )
    p.add_argument(
        "--snapshot",
        type=int,
        metavar="N",
        default=None,
        help="Attach to snapshot N. Defaults to latest if omitted.",
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
    p.set_defaults(func=cmd_attach)


def cmd_attach(args: argparse.Namespace) -> int:
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

    # ── Resolve source file
    src_path = Path(args.file).expanduser().resolve()

    # ── Resolve sidecar type
    sidecar_type = SidecarType(args.sidecar_type)

    # ── Run
    result = do_attach(
        paths=paths,
        src_path=src_path,
        sidecar_type=sidecar_type,
        snapshot_index=args.snapshot,
    )

    if not result.ok:
        for err in result.errors:
            print(f"Error: {err}", file=sys.stderr)
        return 1

    for warning in result.warnings:
        print(f"Warning: {warning}")

    action = "Replaced" if result.overwritten else "Attached"
    print(
        f"{action} '{result.filename}' "
        f"[{result.sidecar_type.value}] "
        f"→ snapshot {result.snapshot_index:03d}"
    )
    return 0
