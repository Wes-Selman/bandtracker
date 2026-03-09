"""
core/diff/interpreter.py

Byte-range interpreter for BandTracker — Increment 3.

Takes the list of ChangedRange objects from engine.byte_diff() and maps
known byte offsets to human-readable change descriptions like
"tempo changed to 124 BPM" or "track muted".

Field offsets come from the band-cartographer research findings
(https://github.com/Wes-Selman/band-cartographer). Only confirmed fields
are decoded — unknown ranges are silently ignored, leaving room for the
map to grow as research continues.

Nothing in here does I/O.
"""

from __future__ import annotations

import struct
from typing import Optional

from core.diff.engine import ChangedRange, DiffResult


# ─────────────────────────────────────────────────────────────
# KNOWN FIELD MAP
# (offsets from band-cartographer research/10.4.8_arm64)
# ─────────────────────────────────────────────────────────────

# Tempo is bpm * 10_000.
# All four confirmed tempo offsets from the change-tempo-1bpm experiment.
# Any one of them changing is enough to report a tempo change — we decode
# whichever one we find first.
_TEMPO_OFFSETS = frozenset({0xaa, 0x102, 0x3be, 0x12cc})

# Pan is stored as a uint32 at this offset (range interpretation TBD
# from research — stored for future use).
_PAN_OFFSET = 0xc5

# Structural change threshold: a size delta larger than this (in bytes)
# almost certainly means a track or region was added or removed rather
# than a simple field edit. Tuned conservatively — real track additions
# in the research data showed deltas of hundreds of bytes.
_STRUCTURAL_CHANGE_THRESHOLD = 64


# ─────────────────────────────────────────────────────────────
# DECODERS
# ─────────────────────────────────────────────────────────────

def _decode_tempo(full_changed: bytes, offset: int) -> Optional[str]:
    """
    Decode a tempo field value and return a human-readable description.

    Reads 4 bytes from the full ProjectData blob at the known tempo
    offset. This is necessary because byte_diff only captures the bytes
    that differ — the range may be shorter than 4 bytes even though the
    field is a uint32 (only the high bytes may change between saves).

    Args:
        full_changed    complete changed ProjectData blob
        offset          byte offset of the tempo field (one of _TEMPO_OFFSETS)

    Returns:
        "tempo changed to 124 BPM" style string, or None if undecodeable.
    """
    if offset + 4 > len(full_changed):
        return None
    try:
        raw = struct.unpack_from("<I", full_changed, offset)[0]
        if raw == 0:
            return None
        bpm = round(raw / 10000.0, 1)
        # Sanity check — GarageBand supports 5–990 BPM
        if not (5 <= bpm <= 990):
            return None
        # Display as integer when it's a whole number
        if bpm == int(bpm):
            return f"tempo changed to {int(bpm)} BPM"
        return f"tempo changed to {bpm} BPM"
    except Exception:
        return None


def _decode_pan(full_changed: bytes, offset: int) -> Optional[str]:
    """
    Decode a pan field value.

    The exact encoding (centre = 0? centre = 64?) is not yet confirmed
    by band-cartographer research. We report the raw value for now.

    Args:
        full_changed    complete changed ProjectData blob
        offset          byte offset of the pan field (_PAN_OFFSET)

    Returns:
        "track pan changed" style string, or None if undecodeable.
    """
    if offset >= len(full_changed):
        return None
    try:
        if offset + 4 <= len(full_changed):
            raw = struct.unpack_from("<I", full_changed, offset)[0]
        else:
            raw = full_changed[offset]
        return f"track pan changed (value: {raw})"
    except Exception:
        return None


def _classify_structural_change(diff_result: DiffResult) -> Optional[str]:
    """
    Detect structural changes (track additions/removals, region edits)
    from the size delta of the diff.

    A large insertion strongly suggests a track or region was added.
    A large deletion suggests one was removed.

    Args:
        diff_result     full DiffResult from byte_diff()

    Returns:
        Human-readable description, or None if no structural change
        is detectable.
    """
    delta = diff_result.size_delta
    if abs(delta) < _STRUCTURAL_CHANGE_THRESHOLD:
        return None

    if delta > 0:
        # Rough heuristic: very large insertions suggest a whole track;
        # medium insertions suggest a region. Thresholds are intentionally
        # conservative until more research data confirms exact record sizes.
        if delta > 1000:
            return "track added"
        return "region added or track added"
    else:
        if abs(delta) > 1000:
            return "track removed"
        return "region removed or track removed"


# ─────────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────────

def interpret_changes(
    diff_result: DiffResult,
    full_changed_bytes: Optional[bytes] = None,
) -> list[str]:
    """
    Map a DiffResult's changed ranges to human-readable descriptions.

    Only confirmed field offsets from band-cartographer research are
    decoded. Unknown ranges are silently ignored so that the output
    stays clean even as more fields are mapped over time.

    Args:
        diff_result         from engine.byte_diff() — must have ok=True
        full_changed_bytes  the complete current ProjectData bytes.
                            Defaults to diff_result.full_changed_bytes.
                            Required for accurate tempo/pan decoding:
                            byte_diff only captures changed bytes, which
                            may be shorter than the full uint32 field.
                            If None, field decoding is skipped and only
                            the structural size-delta check runs.

    Returns:
        List of plain-language change descriptions, e.g.:
            ["tempo changed to 124 BPM", "track pan changed (value: 8)"]
        Empty list if nothing recognisable changed or diff failed.
    """
    if not diff_result.ok:
        return []

    # Prefer explicit argument, fall back to what byte_diff stored
    blob = full_changed_bytes if full_changed_bytes is not None else diff_result.full_changed_bytes

    descriptions: list[str] = []
    tempo_reported = False
    pan_reported = False

    if blob is not None:
        for r in diff_result.changed_ranges:
            offset = r.offset_start

            # Tempo — any of the four confirmed offsets
            if offset in _TEMPO_OFFSETS and not tempo_reported:
                desc = _decode_tempo(blob, offset)
                if desc:
                    descriptions.append(desc)
                    tempo_reported = True
                continue

            # Pan
            if offset == _PAN_OFFSET and not pan_reported:
                desc = _decode_pan(blob, offset)
                if desc:
                    descriptions.append(desc)
                    pan_reported = True
                continue

    # Structural change check — based on overall size delta, not individual ranges
    structural = _classify_structural_change(diff_result)
    if structural:
        descriptions.append(structural)

    return descriptions
