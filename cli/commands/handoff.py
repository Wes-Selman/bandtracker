"""
cli/commands/handoff.py

CLI handler for `bandtracker handoff --to <identifier> [--note "..."] [--force]`.

Passes the ball to a specific collaborator, locking the project to them.
The recipient must already be in the project's collaborators list.

Usage:
    bandtracker handoff --to maya@email.com
    bandtracker handoff --to maya@email.com --note "Bridge needs work"
    bandtracker handoff --to maya@email.com --force
"""

from __future__ import annotations

import argparse
import os
import sys

from core.handoff_ops import do_handoff
from core.models import StorageProvider
from core.init import validate_project_name
from pathlib import Path


def add_subparser(subparsers) -> None:
    p = subparsers.add_parser(
        "handoff",
        help="Pass the project to a collaborator.",
        description=(
            "Lock the project to a specific collaborator, signalling that "
            "they have the ball. The recipient must already be in the "
            "project's collaborators list."
        ),
    )
    p.add_argument(
        "--to",
        required=True,
        metavar="IDENTIFIER",
        dest="to_identifier",
        help="Email or identifier of the collaborator to hand off to.",
    )
    p.add_argument(
        "--note",
        metavar="MESSAGE",
        default=None,
        help='Optional message for the recipient, e.g. "Bridge needs work".',
    )
    p.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Override an existing lock without error.",
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
    result = do_handoff(
        provider=provider,
        project_name=project_name,
        author=author,
        to_identifier=args.to_identifier,
        note=args.note,
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
