"""
core/diff/noise.py

Noise mask management for BandTracker — Increment 3.

GarageBand rewrites ~18,600 byte ranges on every save regardless of
what the user actually changed — timestamps, session counters, internal
checksums. Without filtering these out, every diff is buried in noise.

The noise mask is built once by diffing two identical saves of the same
project (no intentional changes). It is stored as noise_mask.json in the
project root and loaded automatically on every subsequent diff.

Ported from band_cartographer.py (load_noise_mask, save_noise_mask,
cmd_learn_noise). The build logic is used by the learn_noise CLI command
(cli/commands/learn_noise.py) — not by the snapshot flow.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.diff.engine import byte_diff


# ─────────────────────────────────────────────────────────────
# LOAD / SAVE
# ─────────────────────────────────────────────────────────────

def load_noise_mask(noise_mask_path: Path) -> set[int]:
    """
    Load the noise mask from noise_mask.json.

    Returns a set of integer byte offsets to suppress during diffing.
    Returns an empty set (no filtering) if the file doesn't exist or
    can't be parsed — this degrades gracefully rather than crashing.

    Args:
        noise_mask_path     path to noise_mask.json
                            (ProjectPaths.noise_mask_json)
    """
    if not noise_mask_path.exists():
        return set()
    try:
        data = json.loads(noise_mask_path.read_text())
        return set(data.get("noisy_offsets", []))
    except Exception:
        # Corrupt or unreadable mask — proceed unfiltered
        return set()


def save_noise_mask(
    noise_mask_path: Path,
    noisy_offsets: set[int],
    meta: Optional[dict] = None,
) -> None:
    """
    Save the noise mask to noise_mask.json.

    Args:
        noise_mask_path     where to write the file
        noisy_offsets       set of byte offsets to suppress
        meta                optional dict of provenance info
                            (GarageBand version, source files, etc.)
    """
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "description": (
            "Offsets that GarageBand rewrites on every save regardless of "
            "user action (timestamps, session counters, checksums, etc). "
            "These are filtered out of all diffs automatically."
        ),
        "noisy_offset_count": len(noisy_offsets),
        "noisy_offsets": sorted(noisy_offsets),
        "meta": meta or {},
    }
    noise_mask_path.write_text(json.dumps(payload, indent=2))


# ─────────────────────────────────────────────────────────────
# BUILD
# ─────────────────────────────────────────────────────────────

def build_noise_mask(
    baseline_bytes: bytes,
    noise_sample_bytes: bytes,
) -> set[int]:
    """
    Discover which byte positions GarageBand always rewrites by diffing
    two saves of the same project with no intentional changes.

    Every byte that differs between them is noise.

    Args:
        baseline_bytes      raw ProjectData bytes from the opened baseline
        noise_sample_bytes  raw ProjectData bytes from a no-change re-save

    Returns:
        Set of integer byte offsets covering all changed positions.
        Pass this to save_noise_mask() to persist it.
    """
    # Raw diff — no mask applied, we ARE building the mask
    raw = byte_diff(baseline_bytes, noise_sample_bytes, noise_mask=None)

    noisy_offsets: set[int] = set()
    for r in raw.changed_ranges:
        for offset in range(r.offset_start, r.offset_end):
            noisy_offsets.add(offset)

    return noisy_offsets
