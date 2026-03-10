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
import sys

from core.handoff_ops import do_handoff
from core.init import validate_project_name
from cli.resolver import make_provider, resolve_project, resolve_author


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
            "Defaults to BANDTRACKER_PROJECT env var or auto-detect."
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
    provider = make_provider(args.root)

    # ── Resolve project name ───────────────────────────────────
    project_name = resolve_project(provider, args.project)
    if not project_name:
        return 1

    try:
        validate_project_name(project_name)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # ── Resolve author ─────────────────────────────────────────
    author = resolve_author(args.author)
    if not author:
        print(
            "Error: author identifier required. "
            "Pass --author or set BANDTRACKER_AUTHOR.",
            file=sys.stderr,
        )
        return 1

    # ── Run ────────────────────────────────────────────────────
    result = do_handoff(
        provider=provider,
        project_name=project_name,
        author=author,
        to_identifier=args.to_identifier,
        note=args.note,
        force=args.force,
    )

    if not result.ok:
        for e in result.errors:
            print(f"Error: {e}", file=sys.stderr)
        return 1

    for w in result.warnings:
        print(f"Warning: {w}")

    print(result.summary)
    return 0
