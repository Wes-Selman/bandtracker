"""
cli/commands/snapshot.py

CLI command: bandtracker snapshot

Usage:
    bandtracker snapshot -m "Verse structure done"
    bandtracker snapshot -m "Bridge locked in" --milestone arrangement_lock
    bandtracker snapshot   # uses placeholder description

This module only handles argument parsing and output formatting.
All business logic lives in core/snapshot.py.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from core.models import MilestoneTag, StorageProvider
from core.snapshot import take_snapshot, PLACEHOLDER_DESCRIPTION

# ─────────────────────────────────────────────────────────────
# MILESTONE TAG LOOKUP
# ─────────────────────────────────────────────────────────────

_MILESTONE_MAP: dict[str, MilestoneTag] = {
    tag.value: tag for tag in MilestoneTag
}


def _parse_milestone(value: str) -> MilestoneTag:
    """Convert a CLI string to a MilestoneTag, or raise ArgumentTypeError."""
    tag = _MILESTONE_MAP.get(value.lower())
    if tag is None:
        valid = ", ".join(_MILESTONE_MAP.keys())
        raise argparse.ArgumentTypeError(
            f"Unknown milestone '{value}'. Valid options: {valid}"
        )
    return tag


# ─────────────────────────────────────────────────────────────
# ARGUMENT PARSER
# ─────────────────────────────────────────────────────────────

def build_parser(subparsers=None) -> argparse.ArgumentParser:
    """
    Build the argument parser for `bandtracker snapshot`.

    Can be called standalone (returns a top-level parser) or
    registered into an existing subparsers group.
    """
    kwargs = dict(
        description="Take a snapshot of the current project state.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  bandtracker snapshot -m "Verse structure done"
  bandtracker snapshot -m "Bridge locked" --milestone arrangement_lock
  bandtracker snapshot --milestone final_mix -m "This is the one"
  bandtracker snapshot   # saves with a placeholder description

Milestone tags:
  arrangement_lock   The arrangement is frozen — no structural changes
  final_mix          This is the finished mix
  handoff            Project is being passed to a collaborator
        """,
    )

    if subparsers is not None:
        parser = subparsers.add_parser("snapshot", **kwargs)
    else:
        parser = argparse.ArgumentParser(prog="bandtracker snapshot", **kwargs)

    parser.add_argument(
        "-m", "--message",
        metavar="MESSAGE",
        default=None,
        help="Description of what changed. Defaults to a placeholder.",
    )
    parser.add_argument(
        "--milestone",
        metavar="TAG",
        type=_parse_milestone,
        default=None,
        help=(
            "Optional milestone tag: arrangement_lock, final_mix, handoff. "
            "At most one per snapshot."
        ),
    )
    parser.add_argument(
        "--project",
        metavar="NAME",
        default=None,
        help=(
            "Project name to snapshot. If omitted, BandTracker tries to "
            "detect the project in the current directory."
        ),
    )
    parser.add_argument(
        "--author",
        metavar="EMAIL",
        default=None,
        help="Identifier of the person taking the snapshot (email/Apple ID).",
    )
    parser.add_argument(
        "--root",
        metavar="PATH",
        type=Path,
        default=Path.home() / "BandTracker",
        help="BandTracker root folder. Defaults to ~/BandTracker.",
    )

    return parser


# ─────────────────────────────────────────────────────────────
# COMMAND HANDLER
# ─────────────────────────────────────────────────────────────

def run(args: argparse.Namespace) -> int:
    """
    Execute the snapshot command. Returns an exit code (0 = success).

    This function is designed to be called from the main CLI entry
    point after argument parsing.
    """
    provider = StorageProvider.local(args.root)

    # Resolve project name — either from --project flag or auto-detect
    project_name = args.project
    if not project_name:
        project_name = _detect_project(provider)
        if not project_name:
            print(
                "error: Could not detect a BandTracker project. "
                "Use --project NAME to specify one.",
                file=sys.stderr,
            )
            return 1

    # Resolve author — either from --author flag or fall back to owner in project.json
    author = args.author
    if not author:
        author = _detect_author(provider, project_name)
        if not author:
            print(
                "error: Could not detect author. Use --author EMAIL.",
                file=sys.stderr,
            )
            return 1

    result = take_snapshot(
        provider=provider,
        project_name=project_name,
        author=author,
        message=args.message,
        milestone=args.milestone,
    )

    if not result.ok:
        for err in result.errors:
            print(f"error: {err}", file=sys.stderr)
        return 1

    # ── Success output ─────────────────────────────────────────
    milestone_str = ""
    if args.milestone:
        milestone_str = f"  [{args.milestone.value}]"

    print(
        f"✓ Snapshot {result.snapshot_index:03d} saved{milestone_str}\n"
        f"  {result.description}\n"
        f"  {result.media_files_copied} media file(s) added to store, "
        f"{result.media_files_deduped} already present"
    )

    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)

    return 0


# ─────────────────────────────────────────────────────────────
# AUTO-DETECTION HELPERS
# ─────────────────────────────────────────────────────────────

def _detect_project(provider: StorageProvider) -> str | None:
    """
    If there is exactly one project in the BandTracker root, use it.
    Returns None if zero or more than one project exists.
    """
    projects_path = provider.projects_path
    if not projects_path.exists():
        return None
    projects = [
        d.name for d in projects_path.iterdir()
        if d.is_dir() and (d / "project.json").exists()
    ]
    return projects[0] if len(projects) == 1 else None


def _detect_author(provider: StorageProvider, project_name: str) -> str | None:
    """
    Fall back to the project owner as the author if --author is not given.
    """
    from core.models import Project, ProjectPaths
    project_root = provider.project_path(project_name)
    paths = ProjectPaths(project_root)
    if not paths.project_json.exists():
        return None
    try:
        project = Project.from_json(paths.project_json.read_text())
        return project.owner
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
# STANDALONE ENTRY POINT
# ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(run(args))


if __name__ == "__main__":
    main()
