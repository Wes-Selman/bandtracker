"""
cli/resolver.py

Shared CLI resolution helpers for BandTracker — Increment 9.

Every command that accepts --root, --project, or --author flags
imports from here instead of duplicating the resolution logic.

Resolution order (per architectural constraint #5):
    flags → env vars → auto-infer → error

No config file reading — that lands in Increment 13.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from core.models import Project, ProjectPaths, StorageProvider


# ─────────────────────────────────────────────────────────────
# PROVIDER
# ─────────────────────────────────────────────────────────────

def make_provider(root_arg: Optional[str | Path] = None) -> StorageProvider:
    """
    Construct a StorageProvider from the first available source:
      1. --root flag (root_arg)
      2. BANDTRACKER_ROOT env var
      3. ~/BandTracker

    Uses StorageProvider.detect() to identify the backend type from
    the path (iCloud, Dropbox, etc.) — backward-compatible with
    .local() for plain local paths.
    """
    if root_arg is not None:
        root_str = str(root_arg)
    else:
        root_str = os.environ.get("BANDTRACKER_ROOT", str(Path.home() / "BandTracker"))
    root = Path(root_str).expanduser().resolve()
    return StorageProvider.detect(root)


# ─────────────────────────────────────────────────────────────
# PROJECT NAME
# ─────────────────────────────────────────────────────────────

def resolve_project(
    provider: StorageProvider,
    project_arg: Optional[str] = None,
) -> Optional[str]:
    """
    Resolve the project name from the first available source:
      1. --project flag (project_arg)
      2. BANDTRACKER_PROJECT env var
      3. Auto-detect if exactly one project exists
      4. Error

    Prints to stderr and returns None on failure.
    """
    name = project_arg or os.environ.get("BANDTRACKER_PROJECT")
    if name:
        if not provider.project_path(name).exists():
            print(
                f"Error: Project '{name}' not found in {provider.projects_path}.",
                file=sys.stderr,
            )
            return None
        return name

    # Auto-detect
    projects_root = provider.projects_path
    if not projects_root.exists():
        print(f"Error: No projects found in {projects_root}.", file=sys.stderr)
        return None

    candidates = sorted(d.name for d in projects_root.iterdir() if d.is_dir())

    if len(candidates) == 1:
        return candidates[0]

    if len(candidates) == 0:
        print(f"Error: No projects found in {projects_root}.", file=sys.stderr)
        return None

    print(
        "Error: Multiple projects found. "
        "Use --project NAME or set BANDTRACKER_PROJECT.",
        file=sys.stderr,
    )
    return None


# ─────────────────────────────────────────────────────────────
# AUTHOR
# ─────────────────────────────────────────────────────────────

def resolve_author(author_arg: Optional[str] = None) -> Optional[str]:
    """
    Resolve author from --author flag or BANDTRACKER_AUTHOR env var.
    Returns None if neither is set (caller decides how to handle).
    Does NOT print — some commands fall back to project owner.
    """
    return author_arg or os.environ.get("BANDTRACKER_AUTHOR")


def detect_author(
    provider: StorageProvider,
    project_name: str,
) -> Optional[str]:
    """
    Fall back to the project owner when --author is not provided.
    Returns None if project.json can't be read.
    """
    project_root = provider.project_path(project_name)
    paths = ProjectPaths(project_root)
    if not paths.project_json.exists():
        return None
    try:
        project = Project.from_json(paths.project_json.read_text())
        return project.owner
    except Exception:
        return None
