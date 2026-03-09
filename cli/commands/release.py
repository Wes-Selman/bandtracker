"""
cli/commands/release.py

CLI handler for `bandtracker release [--force]`.

Returns the project to the idle/open state — neither collaborator
holds the ball. Safe to call when you're done working but don't
want to hand off to a specific person.

Usage:
    bandtracker release
    bandtracker release --force        # release even if locked to someone else
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from core.handoff_ops import do_release
from core.models import StorageProvider
from core.init import validate_project_name


def add_subparser(subparsers) -> None:
    p = subparsers.add_parser(
        "release",
        help="Release the project lock, returning it to open/idle state.",
        description=(
            "Return the project to the open state — neither collaborator "
            "holds the ball. Use this when you are done working but are "
            "not handing off to a specific person."
        ),
    )
    p.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Release even if the lock is held by someone else.",
    )
    p.add_argument(
        "--project",
        metavar="NAME",
        default=None,
        help=(
            "Project name (folder name under projects/). "
            "Defaults to BANDTRACKER_PROJECT env var."
        ),
    )
    p.add_argument(
        "--root",
        metavar="PATH",
        default=None,
        help=(
            "BandTracker root folder. "
            "Defaults to BANDTRACKER_ROOT env var or ~/BandTracker."
        ),
    )
    p.add_argument(
        "--author",
        metavar="IDENTIFIER",
        default=None,
        help=(
            "Your identifier (email). "
            "Defaults to BANDTRACKER_AUTHOR env var."
        ),
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    # ── Resolve root ───────────────────────────────────────────
    root_str = args.root or os.environ.get("BANDTRACKER_ROOT")
    if root_str:
        root = Path(root_str).expanduser().resolve()
    else:
        root = Path("~/BandTracker").expanduser()

    provider = StorageProvider.detect(root)

    # ── Resolve project name ───────────────────────────────────
    project_name = args.project or os.environ.get("BANDTRACKER_PROJECT")
    if not project_name:
        print(
            "Error: project name required. "
            "Pass --project or set BANDTRACKER_PROJECT.",
            file=sys.stderr,
        )
        return 1

    try:
        validate_project_name(project_name)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # ── Resolve author ─────────────────────────────────────────
    author = args.author or os.environ.get("BANDTRACKER_AUTHOR")
    if not author:
        print(
            "Error: author identifier required. "
            "Pass --author or set BANDTRACKER_AUTHOR.",
            file=sys.stderr,
        )
        return 1

    # ── Execute ────────────────────────────────────────────────
    result = do_release(
        provider=provider,
        project_name=project_name,
        author=author,
        force=args.force,
    )

    for warning in result.warnings:
        print(f"Warning: {warning}")

    if not result.ok:
        for error in result.errors:
            print(f"Error: {error}", file=sys.stderr)
        return 1

    print(f"✓ {result.summary}")
    return 0
