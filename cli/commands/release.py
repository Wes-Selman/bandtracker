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
import sys

from core.handoff_ops import do_release
from core.init import validate_project_name
from cli.resolver import make_provider, resolve_project, resolve_author


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
    result = do_release(
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
