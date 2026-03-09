"""
core/bundle_ref.py

Utilities for storing and resolving the path to a GarageBand .band
bundle across sessions, including resilience to moves on the same
volume via macOS NSURL bookmarks (aliases).

Public API:
    make_alias(path)
        → Optional[str]   base64-encoded bookmark data, or None on non-macOS

    resolve_alias(alias_b64)
        → Optional[Path]  resolved path, or None if stale / non-macOS

    resolve_gb_bundle(gb_bundle_path, gb_bundle_alias)
        → tuple[Optional[Path], str]  (resolved_path, error_message)
        Try alias first, fall back to stored path string.
        Returns (None, error) if both fail.

    store_bundle_ref(path)
        → tuple[str, Optional[str]]   (path_str, alias_b64_or_None)
        Convenience for storing both fields at init / set-gb time.

Design notes:
  - All macOS-specific code is isolated to this module.
  - On non-macOS platforms make_alias returns None and resolve_alias
    returns None; the rest of the codebase treats alias as optional.
  - We use PyObjC (Foundation framework) for bookmark creation when
    available. If PyObjC is not installed we skip alias creation
    silently — the stored path is the fallback.
  - Bookmark data is stored as base64 so it survives JSON round-trips.
  - resolve_alias updates the stored path when the bookmark resolves
    to a new location (the file moved). Callers are responsible for
    persisting that update.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path
from typing import Optional


# ─────────────────────────────────────────────────────────────
# PLATFORM GUARD
# ─────────────────────────────────────────────────────────────

_IS_MACOS = sys.platform == "darwin"


# ─────────────────────────────────────────────────────────────
# ALIAS CREATION
# ─────────────────────────────────────────────────────────────

def make_alias(path: Path) -> Optional[str]:
    """
    Create a macOS NSURL security-scoped bookmark for path.
    Returns base64-encoded bookmark data as a string, or None
    if not on macOS, PyObjC is unavailable, or creation fails.

    The bookmark encodes the file's inode identity so it can be
    resolved even if the file is moved within the same volume.
    """
    if not _IS_MACOS:
        return None

    try:
        from Foundation import NSURL, NSURLBookmarkCreationOptions  # type: ignore

        url = NSURL.fileURLWithPath_(str(path.expanduser().resolve()))
        if url is None:
            return None

        # NSURLBookmarkCreationSuitableForBookmarkFile = 0x400
        # This is the standard "alias file" bookmark type — persistent
        # across renames and moves on the same volume.
        bookmark_data, error = url.bookmarkDataWithOptions_includingResourceValuesForKeys_relativeToURL_error_(
            0x400,   # NSURLBookmarkCreationSuitableForBookmarkFile
            None,
            None,
            None,
        )

        if error is not None or bookmark_data is None:
            return None

        raw_bytes = bytes(bookmark_data)
        return base64.b64encode(raw_bytes).decode("ascii")

    except Exception:
        # PyObjC not installed, sandbox restrictions, or any other failure.
        # Degrade silently — stored path is the fallback.
        return None


# ─────────────────────────────────────────────────────────────
# ALIAS RESOLUTION
# ─────────────────────────────────────────────────────────────

def resolve_alias(alias_b64: str) -> Optional[Path]:
    """
    Resolve a base64-encoded NSURL bookmark back to a Path.
    Returns the resolved Path, or None if:
      - not on macOS
      - PyObjC unavailable
      - bookmark is stale (file deleted, volume unmounted, etc.)
      - any other error

    The returned path reflects the file's current location, even if
    it has been moved since the bookmark was created.
    """
    if not _IS_MACOS:
        return None

    try:
        from Foundation import NSURL, NSData  # type: ignore

        raw_bytes = base64.b64decode(alias_b64)
        ns_data = NSData.dataWithBytes_length_(raw_bytes, len(raw_bytes))
        if ns_data is None:
            return None

        # NSURLBookmarkResolutionWithoutUI = 0x100
        # NSURLBookmarkResolutionWithoutMounting = 0x200
        resolved_url, is_stale, error = NSURL.URLByResolvingBookmarkData_options_relativeToURL_bookmarkDataIsStale_error_(
            ns_data,
            0x100 | 0x200,
            None,
            None,
            None,
        )

        if error is not None or resolved_url is None:
            return None

        path_str = resolved_url.path()
        if path_str is None:
            return None

        return Path(path_str)

    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
# RESOLUTION WITH FALLBACK
# ─────────────────────────────────────────────────────────────

def resolve_gb_bundle(
    gb_bundle_path: Optional[str],
    gb_bundle_alias: Optional[str],
) -> tuple[Optional[Path], str]:
    """
    Resolve the GarageBand bundle path using the best available method.

    Resolution order:
      1. If alias is present, try to resolve it (handles moves).
      2. Fall back to the stored path string.
      3. If both fail, return (None, error_message).

    Returns:
        (path, "")       — resolved successfully
        (None, message)  — both methods failed; message describes why

    The caller is responsible for updating project.json if the alias
    resolved to a different path than the stored string (i.e. the
    file was moved).
    """
    # ── Try alias first ───────────────────────────────────────
    if gb_bundle_alias:
        resolved = resolve_alias(gb_bundle_alias)
        if resolved is not None and resolved.exists():
            return resolved, ""

    # ── Fall back to stored path ──────────────────────────────
    if gb_bundle_path:
        p = Path(gb_bundle_path).expanduser()
        if p.exists():
            return p, ""
        return None, (
            f"GarageBand bundle not found at stored path: {gb_bundle_path}\n"
            f"Run `bandtracker set-gb <project> --gb <path>` to update it."
        )

    # ── Nothing stored ────────────────────────────────────────
    return None, (
        "No GarageBand bundle path stored for this project.\n"
        "Run `bandtracker set-gb <project> --gb <path>` to set it, "
        "or pass --gb on the command line."
    )


# ─────────────────────────────────────────────────────────────
# CONVENIENCE: STORE BOTH FIELDS
# ─────────────────────────────────────────────────────────────

def store_bundle_ref(path: Path) -> tuple[str, Optional[str]]:
    """
    Given a resolved .band bundle path, return the values to store
    in project.json:
        (gb_bundle_path, gb_bundle_alias)

    gb_bundle_path  — str with ~ unexpanded for portability.
                      We store the home-relative form when possible
                      so the path is meaningful on other machines.
    gb_bundle_alias — base64 bookmark string, or None on non-macOS
                      or if PyObjC is unavailable.
    """
    resolved = path.expanduser().resolve()

    # Store as ~/... when the path is under the user's home directory
    # so the project.json is less machine-specific.
    try:
        rel = resolved.relative_to(Path.home())
        path_str = "~/" + str(rel)
    except ValueError:
        path_str = str(resolved)

    alias = make_alias(resolved)
    return path_str, alias
