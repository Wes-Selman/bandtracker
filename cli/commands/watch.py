"""
cli/commands/watch.py

CLI handler for `bandtracker watch <project>`.

Thin layer — all logic is in core/watcher.py.
This module only handles:
  - Argument parsing
  - Resolving the GB bundle path
  - Pretty-printing startup info
  - Ctrl+C handling
  - Exit codes

Usage:
    bandtracker watch MidnightDrive --gb ~/Music/GarageBand/MidnightDrive.band
    bandtracker watch MidnightDrive --gb ~/Music/GarageBand/MidnightDrive.band --auto
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from core.models import StorageProvider
from core.watcher import ProjectWatcher, preflight


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register the 'watch' subcommand."""
    p = subparsers.add_parser(
        "watch",
        help="Watch a GarageBand project and prompt to save versions",
        description=(
            "Monitors the live GarageBand .band bundle for saves, "
            "runs the diff engine, and prompts 'Save a version? [y/n]' "
            "each time GarageBand writes to disk."
        ),
    )
    p.add_argument(
        "project",
        help="BandTracker project name (matches the folder in projects/)",
    )
    p.add_argument(
        "--gb",
        dest="gb_band_path",
        required=True,
        metavar="PATH",
        help="Path to the original GarageBand .band bundle that GarageBand saves to",
    )
    p.add_argument(
        "--author",
        dest="author",
        default=None,
        metavar="IDENTIFIER",
        help="Your identifier (email). Defaults to project owner if omitted.",
    )
    p.add_argument(
        "--auto",
        dest="auto_yes",
        action="store_true",
        default=False,
        help=(
            "Automatically save a snapshot on every detected save "
            "without prompting. Useful for fully automated recording sessions."
        ),
    )
    p.add_argument(
        "--root",
        dest="root",
        default=None,
        metavar="PATH",
        help=(
            "BandTracker root folder (default: ~/BandTracker). "
            "Override if you initialised BandTracker at a custom location."
        ),
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    """Entry point called by cli/main.py."""

    # ── Resolve BandTracker root ──────────────────────────────
    root = Path(args.root).expanduser() if args.root else Path.home() / "BandTracker"
    provider = StorageProvider.detect(root)

    # ── Resolve GB bundle ─────────────────────────────────────
    gb_band_path = Path(args.gb_band_path).expanduser().resolve()

    # ── Preflight ─────────────────────────────────────────────
    pre = preflight(provider, args.project, gb_band_path)

    if pre.warnings:
        for w in pre.warnings:
            print(f"[warning] {w}", file=sys.stderr)

    if not pre.ok:
        for e in pre.errors:
            print(f"[error] {e}", file=sys.stderr)
        return 1

    # ── Resolve author ────────────────────────────────────────
    author = args.author
    if not author:
        try:
            from core.models import Project, ProjectPaths
            paths = ProjectPaths(provider.project_path(args.project))
            project = Project.from_json(paths.project_json.read_text())
            author = project.owner
        except Exception:
            author = "unknown"

    # ── Start watcher ─────────────────────────────────────────
    watcher = ProjectWatcher(
        provider=provider,
        project_name=args.project,
        author=author,
        gb_band_path=gb_band_path,
        auto_yes=args.auto_yes,
    )

    try:
        watcher.start()
        watcher.join()
    except KeyboardInterrupt:
        print("\nStopping watcher…")
    finally:
        watcher.stop()

    return 0
