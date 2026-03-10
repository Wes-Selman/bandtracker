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
import os
import sys
from pathlib import Path

from core.init import validate_project_name
from core.models import ProjectPaths, StorageProvider
from core.sidecar import do_detach


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
    provider = _make_provider(args.root)

    # ── Resolve project name
    project_name = _resolve_project_name(provider, args.project)
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


# ─────────────────────────────────────────────────────────────
# HELPERS (mirrors attach.py exactly)
# ─────────────────────────────────────────────────────────────

def _make_provider(root_arg: str | None) -> StorageProvider:
    root_str = (
        root_arg
        or os.environ.get("BANDTRACKER_ROOT")
        or str(Path.home() / "BandTracker")
    )
    return StorageProvider.local(Path(root_str).expanduser())


def _resolve_project_name(
    provider: StorageProvider,
    project_arg: str | None,
) -> str | None:
    name = project_arg or os.environ.get("BANDTRACKER_PROJECT")
    if name:
        if not provider.project_path(name).exists():
            print(
                f"Error: Project '{name}' not found in {provider.projects_path}.",
                file=sys.stderr,
            )
            return None
        return name

    projects_root = provider.projects_path
    if not projects_root.exists():
        print(f"Error: No projects found in {projects_root}.", file=sys.stderr)
        return None

    candidates = sorted(d.name for d in projects_root.iterdir() if d.is_dir())
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) == 0:
        print(f"Error: No projects found in {projects_root}.", file=sys.stderr)
        return None

    print("Multiple projects found. Choose one:")
    for i, name in enumerate(candidates, 1):
        print(f"  {i}. {name}")
    try:
        choice = input("Enter number: ").strip()
        idx = int(choice) - 1
        return candidates[idx]
    except (ValueError, IndexError, EOFError, KeyboardInterrupt):
        print("Invalid selection. Aborted.", file=sys.stderr)
        return None
