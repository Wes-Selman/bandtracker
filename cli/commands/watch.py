"""
cli/commands/watch.py

CLI handler for `bandtracker watch <project>`.

Thin layer — all logic is in core/watcher.py.
This module only handles:
  - Argument parsing
  - Resolving the GB bundle path (from project.json or --gb override)
  - Pretty-printing startup info
  - Ctrl+C handling
  - Exit codes

Usage:
    bandtracker watch MidnightDrive
    bandtracker watch MidnightDrive --gb ~/Music/GarageBand/MidnightDrive.band
    bandtracker watch MidnightDrive --auto
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from core.models import Project, ProjectPaths, StorageProvider
from core.bundle_ref import resolve_gb_bundle
from core.watcher import ProjectWatcher, preflight


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register the 'watch' subcommand."""
    p = subparsers.add_parser(
        "watch",
        help="Watch a GarageBand project and prompt to save versions",
        description=(
            "Monitors the live GarageBand .band bundle for saves, "
            "runs the diff engine, and prompts 'Save a version? [y/n]' "
            "each time GarageBand writes to disk. "
            "The GB bundle path is read from project.json; use --gb to override."
        ),
    )
    p.add_argument(
        "project",
        help="BandTracker project name (matches the folder in projects/)",
    )
    p.add_argument(
        "--gb",
        dest="gb_band_path",
        required=False,
        default=None,
        metavar="PATH",
        help=(
            "Path to the original GarageBand .band bundle that GarageBand saves to. "
            "Overrides the path stored in project.json. "
            "Required for projects not yet migrated with `set-gb`."
        ),
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

    # ── Resolve author ────────────────────────────────────────
    author = args.author
    project = None
    try:
        paths = ProjectPaths(provider.project_path(args.project))
        project = Project.from_json(paths.project_json.read_text())
        if not author:
            author = project.owner
    except Exception:
        pass
    if not author:
        author = "unknown"

    # ── Resolve GB bundle path ────────────────────────────────
    if args.gb_band_path:
        # Explicit --gb always wins
        gb_band_path = Path(args.gb_band_path).expanduser().resolve()
    elif project is not None:
        # Try stored path / alias from project.json
        gb_band_path, resolve_err = resolve_gb_bundle(
            project.gb_bundle_path,
            project.gb_bundle_alias,
        )
        if gb_band_path is None:
            print(
                f"[error] {resolve_err}\n"
                f"        Run `bandtracker set-gb {args.project} --gb <path>` "
                f"to store the GB bundle path, or pass --gb on the command line.",
                file=sys.stderr,
            )
            return 1
    else:
        print(
            f"[error] Could not load project.json for '{args.project}'.\n"
            f"        Pass --gb to specify the GarageBand bundle path directly.",
            file=sys.stderr,
        )
        return 1

    # ── Preflight ─────────────────────────────────────────────
    pre = preflight(provider, args.project, gb_band_path)

    if pre.warnings:
        for w in pre.warnings:
            print(f"[warning] {w}", file=sys.stderr)

    if not pre.ok:
        for e in pre.errors:
            print(f"[error] {e}", file=sys.stderr)
        return 1

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
