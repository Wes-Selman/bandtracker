"""
cli/commands/claim.py

CLI handler for `bandtracker claim [--force]`.

Picks up an idle (Open) project, signalling to collaborators that
you have the ball. The project must not be locked unless --force is used.

Usage:
    bandtracker claim
    bandtracker claim --force        # override an existing claim
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from core.handoff_ops import do_claim
from core.models import StorageProvider
from core.init import validate_project_name


def add_subparser(subparsers) -> None:
    p = subparsers.add_parser(
        "claim",
        help="Claim the project, signalling you are now working on it.",
        description=(
            "Lock the project to yourself, signalling to collaborators "
            "that you have the ball. The project should be in the open "
            "state — use --force to override an existing claim."
        ),
    )
    p.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Claim even if the project is already locked to someone else.",
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
    result = do_claim(
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
