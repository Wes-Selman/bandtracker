"""
cli/commands/attachments.py — `bandtracker attachments`

Usage
-----
  bandtracker attachments [--snapshot N] [--all]
                          [--project NAME] [--root PATH]

  (no flags)       Resolved set at the latest snapshot, with inheritance.
  --snapshot N     Resolved set at snapshot N, with inheritance.
  --all            Every attachment across every snapshot, flat list.
                   --snapshot is ignored when --all is set.
  --project NAME   Project name. Inferred if only one project exists.
  --root PATH      BandTracker root (default: ~/BandTracker or
                   BANDTRACKER_ROOT env var).

Inheritance rules:
  version  — shown only for the exact snapshot they are attached to.
  project  — the most recent attachment of the same filename ≤ N wins.
             Older shadowed copies are not shown in resolved mode.
"""

from __future__ import annotations

import argparse
import sys

from core.init import validate_project_name
from core.models import ProjectPaths
from core.sidecar import list_attachments
from cli.resolver import make_provider, resolve_project


def add_subparser(subparsers: argparse._SubParsersAction) -> None:  # noqa: SLF001
    p = subparsers.add_parser(
        "attachments",
        help="List sidecar files attached to a project.",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--snapshot",
        type=int,
        metavar="N",
        default=None,
        help="Show resolved attachments at snapshot N (default: latest).",
    )
    p.add_argument(
        "--all",
        action="store_true",
        dest="all_snapshots",
        help="List every attachment across all snapshots.",
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
    p.set_defaults(func=cmd_attachments)


def cmd_attachments(args: argparse.Namespace) -> int:
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
    result = list_attachments(
        paths=paths,
        snapshot_index=args.snapshot,
        all_snapshots=args.all_snapshots,
    )

    if not result.ok:
        for err in result.errors:
            print(f"Error: {err}", file=sys.stderr)
        return 1

    for warning in result.warnings:
        print(f"Warning: {warning}")

    # ── Display
    _print_attachments(result)
    return 0


def _print_attachments(result) -> None:
    """Render the attachment list to stdout."""
    if result.all_snapshots:
        print("All attachments across all snapshots:")
    else:
        print(f"Attachments at snapshot {result.resolved_at_index:03d}:")

    if not result.items:
        print("  (none)")
        return

    # Column widths
    max_name = max(len(item.filename) for item in result.items)
    max_name = max(max_name, 8)  # minimum "filename" header width

    if result.all_snapshots:
        # Include snapshot column
        print(
            f"  {'filename':<{max_name}}  {'type':<9}  {'snap':>4}  size"
        )
        print(f"  {'-' * max_name}  {'-' * 9}  {'-' * 4}  ----")
        for item in result.items:
            size_str = _format_size(item.size_bytes)
            print(
                f"  {item.filename:<{max_name}}  "
                f"{item.sidecar_type.value:<9}  "
                f"{item.snapshot_index:>4}  "
                f"{size_str}"
            )
    else:
        # Resolved view — snapshot column shows which snap owns the file
        print(
            f"  {'filename':<{max_name}}  {'type':<9}  {'from':>4}  size"
        )
        print(f"  {'-' * max_name}  {'-' * 9}  {'-' * 4}  ----")
        for item in result.items:
            size_str = _format_size(item.size_bytes)
            print(
                f"  {item.filename:<{max_name}}  "
                f"{item.sidecar_type.value:<9}  "
                f"{item.snapshot_index:>4}  "
                f"{size_str}"
            )

    total = len(result.items)
    print(f"\n  {total} attachment{'s' if total != 1 else ''}")


def _format_size(size_bytes: int) -> str:
    """Human-readable file size."""
    if size_bytes == 0:
        return "0 B"
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"
