"""
cli/commands/init.py

CLI handler for 'bandtracker init'.
Thin shell over core/init.py — parses args, calls core, prints results.
"""

from pathlib import Path

from core.init import initialize, validate_band
from core.models import StorageProvider


def run(args):
    band_path = args.band_path.expanduser().resolve()
    root = args.root.expanduser().resolve()
    provider = StorageProvider.detect(root)

    # Owner identity — use --owner flag or prompt
    owner_id = args.owner or _prompt_owner_identifier()
    owner_name = _prompt_owner_name(owner_id)

    print(f"\n  Initializing {band_path.name}...")

    # Pre-flight validation with user-visible detail
    validation = validate_band(band_path)
    for w in validation.warnings:
        print(f"  ⚠  {w}")
    if not validation.ok:
        for e in validation.errors:
            print(f"  ✗  {e}")
        raise SystemExit(1)

    if validation.media_files:
        mb = validation.total_size_bytes // (1024 * 1024)
        print(f"  ·  Found {len(validation.media_files)} audio file(s) ({mb}MB total)")

    # Initialize
    result = initialize(
        band_path=band_path,
        provider=provider,
        owner_identifier=owner_id,
        owner_display_name=owner_name,
    )

    # Surface any non-fatal warnings (e.g. media files that failed to copy)
    for e in result.errors:
        print(f"  ⚠  {e}")

    if not result.ok:
        print(f"  ✗  Initialization failed.")
        raise SystemExit(1)

    print(f"\n  ✓  {result.project_name} is now tracked by BandTracker")
    print(f"  ·  Project root:  {result.project_root}")
    print(f"  ·  Snapshot 001:  Initial version")
    if result.media_files_copied:
        print(f"  ·  Media files:   {result.media_files_copied} copied to store")
    print(f"\n  Open {result.project_name} from BandTracker going forward.")
    print(f"  Run 'bandtracker watch' to start tracking saves.\n")


def _prompt_owner_identifier() -> str:
    val = input("  Your email or name (used in the timeline): ").strip()
    return val if val else "unknown"


def _prompt_owner_name(identifier: str) -> str:
    val = input(f"  Display name [{identifier}]: ").strip()
    return val if val else identifier
