"""
cli/commands/restore.py — `bandtracker restore <n>`

Usage
-----
  bandtracker restore <snapshot_index> [--project NAME] [--force] [--dry-run] [--yes]

  snapshot_index   1-based index of the snapshot to restore to.
  --project NAME   Project name inside the BandTracker projects directory.
                   If omitted, the command uses the project if only one exists,
                   otherwise prompts.
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
from core.models import Project, ProjectPaths, StorageProvider
from core.restore import restore


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

    # ------------------------------------------------------------------
    # Resolve provider (mirrors the pattern used by cmd_snapshot et al.)
    # ------------------------------------------------------------------
    provider = _make_provider()

    # ------------------------------------------------------------------
    # Resolve project name
    # ------------------------------------------------------------------
    project_name = _resolve_project_name(provider, args.project)
    if project_name is None:
        return 1

    # ------------------------------------------------------------------
    # Validate project name (path traversal guard — same as other commands)
    # ------------------------------------------------------------------
    try:
        validate_project_name(project_name)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # ------------------------------------------------------------------
    # Load project for pre-flight display
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Validate target index range before any I/O
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Show plan and confirm
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Run restore
    # ------------------------------------------------------------------
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

def _make_provider() -> StorageProvider:
    """Construct a StorageProvider pointing at ~/BandTracker.

    Matches the pattern used by the other CLI commands — a single
    local provider rooted at ~/BandTracker.  If the project root is
    ever made configurable (e.g. via a config file or env var), this
    is the one place to update for the restore command.
    """
    root = Path.home() / "BandTracker"
    return StorageProvider.local(root)


def _resolve_project_name(
    provider: StorageProvider,
    project_name: str | None,
) -> str | None:
    """Return the project name, prompting if necessary."""
    if project_name:
        project_path = provider.project_path(project_name)
        if not project_path.exists():
            print(
                f"Error: Project '{project_name}' not found in "
                f"{provider.projects_path}.",
                file=sys.stderr,
            )
            return None
        return project_name

    # Auto-detect if exactly one project exists
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

    # Multiple projects — prompt
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
            from core.models import Snapshot
            snap = Snapshot.from_json(target_meta.read_text())
            if snap.description:
                print(f"  Snapshot note   : {snap.description}")
            # timestamp is a datetime object after from_json
            print(f"  Snapshot date   : {snap.timestamp.strftime('%Y-%m-%d %H:%M UTC')}")
        except Exception:
            pass

    print()
    print("  This will:")
    print(f"    • Replace live/ProjectData with the version from snapshot {target:03d}")
    print("    • Take a new snapshot to record this restore event")
    if not dry_run:
        print("  GarageBand must be closed.")
