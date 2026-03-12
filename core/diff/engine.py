"""
core/diff/engine.py

Binary diff engine for BandTracker — Increment 3, updated in Increment 10.

Compares two ProjectData files byte-by-byte, applies the noise mask to
strip spurious GarageBand save noise, and returns a list of meaningful
changed byte ranges. The interpreter layer turns those ranges into
human-readable descriptions.

Ported from band_cartographer.py (diff_inner_bytes, build_commit_message).
Nothing in here does I/O — callers load the bytes and pass them in.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ─────────────────────────────────────────────────────────────
# DATA TYPES
# ─────────────────────────────────────────────────────────────

@dataclass
class ChangedRange:
    """
    A single contiguous span of bytes that differs between two
    ProjectData blobs after noise filtering.

    offset_start    first byte that changed (0-based)
    offset_end      one past the last changed byte (exclusive)
    baseline_bytes  raw bytes from the previous snapshot
    changed_bytes   raw bytes from the current snapshot
    as_uint32_le    little-endian uint32 interpretation of changed_bytes
                    (None if range is shorter than 4 bytes)
    as_float32      little-endian float32 interpretation of changed_bytes
                    (None if range is shorter than 4 bytes or NaN)
    near_string     printable ASCII context extracted around the offset
                    for human debugging
    """
    offset_start: int
    offset_end: int
    baseline_bytes: bytes
    changed_bytes: bytes
    as_uint32_le: Optional[int] = None
    as_float32: Optional[float] = None
    near_string: Optional[str] = None

    @property
    def length(self) -> int:
        return self.offset_end - self.offset_start

    @property
    def offset_hex(self) -> str:
        return hex(self.offset_start)

    def to_dict(self) -> dict:
        return {
            "offset_start": self.offset_start,
            "offset_end":   self.offset_end,
            "length":       self.length,
            "offset_hex":   self.offset_hex,
            "baseline_hex": self.baseline_bytes.hex(),
            "changed_hex":  self.changed_bytes.hex(),
            "as_uint32_le": self.as_uint32_le,
            "as_float32":   f"{self.as_float32:.4f}" if self.as_float32 is not None else None,
            "near_string":  self.near_string,
        }


@dataclass
class DiffResult:
    """
    Full output of a byte diff between two ProjectData blobs.

    changed_ranges          meaningful ranges after noise filtering
    skipped_noisy_ranges    count of ranges suppressed by the noise mask
    baseline_size           byte length of the previous ProjectData
    changed_size            byte length of the current ProjectData
    noise_filtered          True if a noise mask was applied
    full_changed_bytes      the complete current ProjectData bytes,
                            stored here so interpret_changes can read
                            full uint32 fields regardless of range length
    error                   non-None if the diff could not be run
    """
    changed_ranges: list[ChangedRange] = field(default_factory=list)
    skipped_noisy_ranges: int = 0
    baseline_size: int = 0
    changed_size: int = 0
    noise_filtered: bool = False
    full_changed_bytes: Optional[bytes] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def size_delta(self) -> int:
        return self.changed_size - self.baseline_size

    @property
    def num_changed_ranges(self) -> int:
        return len(self.changed_ranges)


# ─────────────────────────────────────────────────────────────
# HELPERS (ported from band_cartographer._try_*)
# ─────────────────────────────────────────────────────────────

def _try_uint32(data: bytes, offset: int) -> Optional[int]:
    try:
        if offset + 4 <= len(data):
            return struct.unpack_from("<I", data, offset)[0]
    except Exception:
        pass
    return None


def _try_float32(data: bytes, offset: int) -> Optional[float]:
    try:
        if offset + 4 <= len(data):
            val = struct.unpack_from("<f", data, offset)[0]
            if val == val:  # exclude NaN
                return val
    except Exception:
        pass
    return None


def _try_string(data: bytes, offset: int, window: int = 64) -> Optional[str]:
    """Extract nearby printable ASCII for debugging context."""
    try:
        chunk = data[max(0, offset - 8): offset + window]
        printable = "".join(chr(b) if 32 <= b < 127 else "·" for b in chunk)
        readable = [s for s in printable.split("·") if len(s) >= 3]
        if readable:
            return " | ".join(readable[:3])
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────────────────────
# CORE DIFF (ported from band_cartographer.diff_inner_bytes)
# ─────────────────────────────────────────────────────────────

def byte_diff(
    baseline: bytes,
    changed: bytes,
    noise_mask: Optional[set] = None,
) -> DiffResult:
    """
    Diff two ProjectData blobs byte-by-byte.

    Args:
        baseline    bytes from the previous snapshot's ProjectData
        changed     bytes from the current live ProjectData
        noise_mask  set of integer byte offsets to suppress
                    (from noise.load_noise_mask). Pass None to skip
                    filtering (raw diff — useful for building the mask).

    Returns:
        DiffResult with changed_ranges populated after noise filtering.
        On error (missing blobs), DiffResult.error is set and
        changed_ranges is empty.
    """
    if baseline is None and changed is None:
        return DiffResult(error="ProjectData missing from both snapshots")
    if baseline is None:
        return DiffResult(error="ProjectData missing from baseline snapshot")
    if changed is None:
        return DiffResult(error="ProjectData missing from current file")

    b_len = len(baseline)
    c_len = len(changed)
    min_len = min(b_len, c_len)

    changed_ranges: list[ChangedRange] = []
    skipped_noisy = 0
    i = 0

    while i < min_len:
        if baseline[i] != changed[i]:
            start = i
            while i < min_len and baseline[i] != changed[i]:
                i += 1
            end = i  # exclusive

            if noise_mask is not None:
                range_offsets = set(range(start, end))

                if range_offsets.issubset(noise_mask):
                    # Entire range is noise — skip it
                    skipped_noisy += 1
                    continue

                # Partially noisy — trim noisy bytes from edges
                clean_start = start
                while clean_start < end and clean_start in noise_mask:
                    clean_start += 1
                clean_end = end
                while clean_end > clean_start and (clean_end - 1) in noise_mask:
                    clean_end -= 1

                if clean_start >= clean_end:
                    skipped_noisy += 1
                    continue

                start, end = clean_start, clean_end

            changed_ranges.append(ChangedRange(
                offset_start=start,
                offset_end=end,
                baseline_bytes=baseline[start:end],
                changed_bytes=changed[start:end],
                as_uint32_le=_try_uint32(changed, start),
                as_float32=_try_float32(changed, start),
                near_string=_try_string(changed, start),
            ))
        else:
            i += 1

    return DiffResult(
        changed_ranges=changed_ranges,
        skipped_noisy_ranges=skipped_noisy,
        baseline_size=b_len,
        changed_size=c_len,
        noise_filtered=noise_mask is not None,
        full_changed_bytes=changed,
    )


# ─────────────────────────────────────────────────────────────
# DESCRIPTION BUILDER (ported from band_cartographer.build_commit_message)
# ─────────────────────────────────────────────────────────────

def build_description(
    diff_result: DiffResult,
    interpreted_changes: list[str],
) -> str:
    """
    Compose a single human-readable description string from
    interpreted changes and raw diff stats.

    Three-tier description quality:

      1. Interpreted — decoded field changes from the interpreter.
         Primary content when available. Uninterpreted range count
         is appended as context.

      2. Structural — when the interpreter returns nothing but byte
         ranges exist. Reports range count, total bytes modified,
         and net size delta. Always producible from DiffResult alone.

      3. Identical — no changed ranges at all.

    Args:
        diff_result         from byte_diff()
        interpreted_changes from interpreter.interpret_changes()

    Returns:
        A plain-language string suitable for Snapshot.description.
    """
    if not diff_result.ok:
        return "diff unavailable"

    # ── Tier 1: Interpreted ────────────────────────────────────
    if interpreted_changes:
        base = "; ".join(interpreted_changes)
        uninterpreted = diff_result.num_changed_ranges - len(interpreted_changes)
        if uninterpreted > 0:
            base += f" (+{uninterpreted} unrecognised change"
            base += "s" if uninterpreted > 1 else ""
            base += ")"
        return base

    # ── Tier 3: Identical ──────────────────────────────────────
    n = diff_result.num_changed_ranges
    if n == 0:
        return "no changes detected"

    # ── Tier 2: Structural ─────────────────────────────────────
    total_modified = sum(r.length for r in diff_result.changed_ranges)
    range_word = "range" if n == 1 else "ranges"
    byte_word = "byte" if total_modified == 1 else "bytes"

    parts = [f"{n} byte {range_word} changed"]
    parts.append(f"{total_modified} {byte_word} modified")

    delta = diff_result.size_delta
    if delta != 0:
        sign = "+" if delta > 0 else ""
        parts.append(f"{sign}{delta} bytes net")

    return ", ".join(parts)
