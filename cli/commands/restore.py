"""
cli/commands/restore.py — `bandtracker restore <n>`

Usage
-----
  bandtracker restore <snapshot_index> [--project NAME] [--root PATH]
                      [--force] [--dry-run] [--yes]

  snapshot_index   1-based index of the snapshot to restore to.
  --project NAME   Project name inside the BandTracker projects directory.
                   If omitted, the command uses the project if only one exists,
                   otherwise errors.
  --root PATH      BandTracker root (default: ~/BandTracker or BANDTRACKER_ROOT).
  --author NAME    Written into the confirmation snapshot (default: $USER).
  --force          Skip the GarageBand-running check (CI / advanced use only).
  --dry-run        Validate all preconditions and print what would happen,
                   but do not write anything.
  --yes / -y       Skip the interactive confirmation prompt.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from core.init import validate_project_name
from core.models import Project, ProjectPaths, Snapshot
from core.restore import restore
from cli.resolver import make_provider, resolve_project


def add_subparser(subparsers: argparse._SubParsersAction) -> None:  # noqa: SLF001
    p = subparsers.add_parser(
        "restore",
        help="Roll the project back to a previous snapshot.",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "snapshot_index",
        type=int,
        metavar="N",
        help="1-based snapshot number to restore to.",
    )
    p.add_argument(
        "--project",
        metavar="NAME",
        help="Project name (default: inferred if only one project exists).",
    )
    p.add_argument(
        "--root",
        metavar="PATH",
        default=None,
        help="BandTracker root directory (default: ~/BandTracker or BANDTRACKER_ROOT).",
    )
    p.add_argument(
        "--author",
        metavar="NAME",
        default=os.environ.get("USER") or os.environ.get("USERNAME") or "unknown",
        help="Name written into the confirmation snapshot (default: current user).",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Skip the GarageBand-running check.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Validate preconditions without writing anything.",
    )
    p.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Skip the confirmation prompt.",
    )
    p.set_defaults(func=cmd_restore)


def cmd_restore(args: argparse.Namespace) -> int:
    """Entry point called by the CLI router."""

    # ── Resolve provider
    provider = make_provider(args.root)

    # ── Resolve project name
    project_name = resolve_project(provider, args.project)
    if project_name is None:
        return 1

    # ── Validate project name
    try:
        validate_project_name(project_name)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # ── Load project for pre-flight display
    project_root = provider.project_path(project_name)
    paths = ProjectPaths(project_root)

    if not paths.project_json.exists():
        print(
            f"Error: {project_root} is not a valid BandTracker project.",
            file=sys.stderr,
        )
        return 1

    try:
        project = Project.from_json(paths.project_json.read_text())
    except Exception as exc:
        print(f"Error: Could not read project.json: {exc}", file=sys.stderr)
        return 1

    # ── Validate target index range before any I/O
    target = args.snapshot_index
    if target < 1:
        print(
            f"Error: Snapshot index must be 1 or greater (got {target}).",
            file=sys.stderr,
        )
        return 1
    if project.latest_snapshot is not None and target > project.latest_snapshot:
        print(
            f"Error: Snapshot {target:03d} does not exist. "
            f"Latest is {project.latest_snapshot:03d}.",
            file=sys.stderr,
        )
        return 1

    # ── Show plan and confirm
    _print_restore_plan(project, paths, target, args.dry_run)

    if not args.dry_run and not args.yes:
        try:
            answer = input("\nProceed with restore? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return 1
        if answer not in ("y", "yes"):
            print("Aborted.")
            return 1

    # ── Run restore
    result = restore(
        provider=provider,
        project_name=project_name,
        target_index=target,
        author=args.author,
        force=args.force,
        dry_run=args.dry_run,
    )

    print()
    print(str(result))

    return 0 if result.success else 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _print_restore_plan(
    project: Project,
    paths: ProjectPaths,
    target: int,
    dry_run: bool,
) -> None:
    """Print a human-readable summary of what restore will do."""
    label = "[DRY RUN] " if dry_run else ""
    print(f"\n{label}Restoring project: {project.name}")
    print(f"  Target snapshot : {target:03d}")
    print(f"  Current snapshot: {project.latest_snapshot:03d}")

    # Show target snapshot's description and date if available
    target_meta = paths.snapshot_meta(target)
    if target_meta.exists():
        try:
            snap = Snapshot.from_json(target_meta.read_text())
            if snap.description:
                print(f"  Snapshot note   : {snap.description}")
            print(f"  Snapshot date   : {snap.timestamp.strftime('%Y-%m-%d %H:%M UTC')}")
        except Exception:
            pass

    print()
    print("  This will:")
    print(f"    • Replace live/ProjectData with the version from snapshot {target:03d}")
    print("    • Take a new snapshot to record this restore event")
    if not dry_run:
        print("  GarageBand must be closed.")
