"""
cli/commands/set_gb.py

CLI handler for `bandtracker set-gb <project> --gb <path>`.

Migration command for projects initialized before Increment 5, which
did not store the GarageBand bundle path in project.json.

Also useful any time the GB bundle has been moved to a new location
on disk and the stored path needs to be updated.

Usage:
    bandtracker set-gb MidnightDrive --gb ~/Music/GarageBand/MidnightDrive.band
    bandtracker set-gb MidnightDrive --gb ~/Music/GarageBand/MidnightDrive.band --root ~/Dropbox/BandTracker
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from core.models import Project, ProjectPaths, StorageProvider
from core.bundle_ref import store_bundle_ref
from core.init import validate_band, validate_project_name, write_json_atomic

_DEFAULT_STORAGE = Path.home() / "BandTracker"


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register the 'set-gb' subcommand."""
    p = subparsers.add_parser(
        "set-gb",
        help="Set or update the GarageBand bundle path stored in project.json",
        description=(
            "Stores the path to the GarageBand .band bundle in project.json "
            "so that `watch` and `reconcile` no longer require --gb. "
            "Run this once for projects initialized before Increment 5, "
            "or any time the .band file has moved."
        ),
    )
    p.add_argument(
        "project",
        help="BandTracker project name (folder inside BandTracker/projects/)",
    )
    p.add_argument(
        "--gb",
        dest="gb_band_path",
        required=True,
        metavar="PATH",
        help="Path to the GarageBand .band bundle",
    )
    p.add_argument(
        "--root",
        dest="root",
        default=None,
        metavar="PATH",
        help=(
            f"BandTracker root folder (default: {_DEFAULT_STORAGE}). "
            "Override if you initialised BandTracker at a custom location."
        ),
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    """Entry point called by cli/main.py."""

    # ── Resolve storage root ──────────────────────────────────
    root = Path(args.root).expanduser() if args.root else _DEFAULT_STORAGE
    provider = StorageProvider.detect(root)

    # ── Resolve and validate GB bundle ────────────────────────
    try:
        validate_project_name(args.project)
    except ValueError as e:
        print(f"[error] {e}", file=__import__('sys').stderr)
        return 1

    gb_band_path = Path(args.gb_band_path).expanduser().resolve()

    validation = validate_band(gb_band_path)
    if not validation.ok:
        for e in validation.errors:
            print(f"[error] {e}", file=sys.stderr)
        return 1
    for w in validation.warnings:
        print(f"[warning] {w}", file=sys.stderr)

    # ── Load project.json ─────────────────────────────────────
    project_root = provider.project_path(args.project)
    paths = ProjectPaths(project_root)

    if not paths.project_json.exists():
        print(
            f"[error] No project named '{args.project}' found at {project_root}.\n"
            f"        Run `bandtracker init` first.",
            file=sys.stderr,
        )
        return 1

    try:
        project = Project.from_json(paths.project_json.read_text())
    except Exception as e:
        print(f"[error] Could not read project.json: {e}", file=sys.stderr)
        return 1

    # ── Warn if overwriting an existing path ──────────────────
    if project.gb_bundle_path and project.gb_bundle_path != str(gb_band_path):
        print(
            f"[info] Updating stored GB path.\n"
            f"       Old: {project.gb_bundle_path}\n"
            f"       New: {gb_band_path}"
        )

    # ── Store path + alias ────────────────────────────────────
    path_str, alias = store_bundle_ref(gb_band_path)
    project.gb_bundle_path = path_str
    project.gb_bundle_alias = alias

    try:
        write_json_atomic(paths.project_json, project.to_json())
    except OSError as e:
        print(f"[error] Could not write project.json: {e}", file=sys.stderr)
        return 1

    # ── Confirm ───────────────────────────────────────────────
    alias_note = " (alias stored)" if alias else " (no alias — path only)"
    print(f"✓ GarageBand bundle path set for '{args.project}':")
    print(f"  {path_str}{alias_note}")
    print()
    print(f"  `bandtracker watch {args.project}` and")
    print(f"  `bandtracker reconcile {args.project}` no longer need --gb.")

    return 0
