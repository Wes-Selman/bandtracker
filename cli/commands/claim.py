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
import sys

from core.handoff_ops import do_claim
from core.init import validate_project_name
from cli.resolver import make_provider, resolve_project, resolve_author


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
    result = do_claim(
        provider=provider,
        project_name=project_name,
        author=author,
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
