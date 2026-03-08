"""
cli/commands/init.py

CLI handler for `bandtracker init <path-to.band>`.

Wraps core/init.py — all business logic lives there.
This file handles argument parsing, user-facing output, and exit codes.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from core.init import initialize
from core.models import StorageProvider


def add_subparser(subparsers) -> None:
    p = subparsers.add_parser(
        "init",
        help="Start tracking a GarageBand project",
        description=(
            "Initialize BandTracker for an existing .band project. "
            "Creates the folder structure, copies the bundle into managed "
            "storage, hashes media files, and takes snapshot 001."
        ),
    )
    p.add_argument(
        "band_path",
        metavar="PATH",
        help="Path to the .band bundle to track",
    )
    p.add_argument(
        "--root",
        metavar="DIR",
        default=str(Path.home() / "BandTracker"),
        help="BandTracker root folder (default: ~/BandTracker)",
    )
    p.add_argument(
        "--name",
        metavar="NAME",
        default=None,
        help="Your name or identifier (default: your system username)",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    """
    Entry point called by cli/main.py.
    Returns exit code: 0 on success, 1 on failure.
    """
    band_path = Path(args.band_path).expanduser().resolve()
    root_path = Path(args.root).expanduser().resolve()

    # Determine owner identifier
    if args.name:
        owner_identifier = args.name
        owner_display_name = args.name
    else:
        import getpass
        owner_identifier = getpass.getuser()
        owner_display_name = getpass.getuser()

    provider = StorageProvider.local(root_path)

    print(f"Initializing {band_path.name}...")

    result = initialize(
        band_path=band_path,
        provider=provider,
        owner_identifier=owner_identifier,
        owner_display_name=owner_display_name,
    )

    if not result.ok:
        for error in result.errors:
            print(f"error: {error}", file=sys.stderr)
        return 1

    # Success output
    print(f"✓ Project '{result.project_name}' initialized")
    print(f"  Location:  {result.project_root}")
    print(f"  Snapshot:  001 — Initial version")
    if result.media_files_copied:
        print(f"  Media:     {result.media_files_copied} file(s) copied to store")

    # Non-fatal warnings (e.g. media copy failures)
    for warning in result.errors:
        print(f"  warning: {warning}")

    print()
    print("Next: open the project in GarageBand, make changes, then run:")
    print(f"  bandtracker snapshot -m \"describe what changed\"")

    return 0
