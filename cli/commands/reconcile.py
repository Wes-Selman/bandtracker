"""
cli/commands/reconcile.py

CLI handler for `bandtracker reconcile <project>`.

Standalone reconciliation — lets the musician check for offline edits
(made while the watcher wasn't running) and optionally snapshot them
before doing other work.

The GarageBand bundle path is read from project.json (stored at init
time or via `bandtracker set-gb`). Pass --gb to override the stored
path, or if this is a pre-Increment-5 project that hasn't been
migrated yet.

Usage:
    bandtracker reconcile MidnightDrive
    bandtracker reconcile MidnightDrive --gb ~/Music/GarageBand/MidnightDrive.band
    bandtracker reconcile MidnightDrive --storage ~/Dropbox/BandTracker
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from core.init import validate_project_name
from core.models import StorageProvider
from core.reconcile import reconcile, ReconcileAction

_DEFAULT_STORAGE = Path.home() / "BandTracker"


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "reconcile",
        help="Check for offline edits and optionally snapshot them.",
        description=(
            "Compares the GarageBand bundle against the latest snapshot. "
            "If they differ, shows what changed and offers to save a version. "
            "The GB bundle path is read from project.json; use --gb to override."
        ),
    )
    p.add_argument(
        "project",
        help="Project name (folder name inside BandTracker/projects/).",
    )
    p.add_argument(
        "--gb",
        dest="gb_band_path",
        default=None,
        metavar="PATH",
        help=(
            "Path to the GarageBand .band bundle. "
            "Overrides the path stored in project.json. "
            "Required for projects not yet migrated with `set-gb`."
        ),
    )
    p.add_argument(
        "--author",
        default=None,
        metavar="EMAIL",
        help="Identifier for the snapshot author (email or Apple ID).",
    )
    p.add_argument(
        "--storage",
        default=None,
        metavar="PATH",
        help=(
            f"BandTracker root directory. "
            f"Defaults to {_DEFAULT_STORAGE}."
        ),
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    storage_root = Path(args.storage).expanduser() if args.storage else _DEFAULT_STORAGE
    provider = StorageProvider.detect(storage_root)
    project_name = args.project

    try:
        validate_project_name(project_name)
    except ValueError as e:
        print(f"[error] {e}", file=__import__('sys').stderr)
        return 1

    # Resolve author: prefer --author flag, fall back to project owner
    author = args.author
    if not author:
        try:
            from core.models import Project, ProjectPaths
            paths = ProjectPaths(provider.project_path(project_name))
            project = Project.from_json(paths.project_json.read_text())
            author = project.owner
        except Exception:
            author = "unknown"

    # Resolve optional --gb override
    gb_band_path = (
        Path(args.gb_band_path).expanduser().resolve()
        if args.gb_band_path
        else None
    )

    result = reconcile(
        provider=provider,
        project_name=project_name,
        author=author,
        gb_band_path=gb_band_path,
    )

    if result.warnings:
        for w in result.warnings:
            print(f"[warning] {w}", file=sys.stderr)

    if result.action == ReconcileAction.CLEAN:
        print(f"✓ {project_name} is clean — GB matches snapshot {result.latest_snapshot_index}.")
        return 0

    if result.action == ReconcileAction.SKIPPED:
        print(f"✓ {project_name} has no snapshots yet — nothing to reconcile.")
        return 0

    if result.action == ReconcileAction.SNAPSHOTTED:
        # Success message already printed by core/reconcile.py
        return 0

    if result.action == ReconcileAction.DEFERRED:
        # Acknowledged message already printed by core/reconcile.py
        return 0

    # ERROR
    for err in result.errors:
        print(f"✗ {err}", file=sys.stderr)

    # If gb_bundle_path is missing, suggest migration
    if "No GarageBand bundle path" in " ".join(result.errors):
        print(
            f"\nTip: run `bandtracker set-gb {project_name} --gb <path>` "
            f"to store the GB bundle path permanently.",
            file=sys.stderr,
        )

    return 1
