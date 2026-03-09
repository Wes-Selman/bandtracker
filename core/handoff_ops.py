"""
core/handoff_ops.py

Handoff state machine for BandTracker — Increment 7.

Implements the three lock transitions:
  do_handoff()  — pass the ball to a specific collaborator (Any → Locked)
  do_release()  — return the project to idle (Locked → Open)
  do_claim()    — pick up an idle project (Open → Locked to self)

Design principles:
  - No I/O other than reading/writing project.json and handoff.json
  - No argparse, no sys.exit — that's the CLI layer's job
  - All path logic through ProjectPaths
  - All disk writes atomic via write_json_atomic()
  - Each function returns a typed result — caller decides how to present it

State machine:

    OPEN ──────── claim(author) ──────────► LOCKED(author)
      ▲                                          │
      │                                          │
    release() ◄──── LOCKED(anyone) ◄──── handoff(--to other)
                         │
                    handoff(--to x)
                    [--force only if
                     already locked]

Collaborator requirement:
  handoff --to <identifier> requires the recipient to already be in
  project.json's collaborators list. This prevents typo-locks and
  ensures both machines agree on who the identifier belongs to.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.models import (
    Collaborator,
    Handoff,
    LockState,
    Project,
    ProjectPaths,
    StorageProvider,
)
from core.init import write_json_atomic


# ─────────────────────────────────────────────────────────────
# RESULT TYPES
# ─────────────────────────────────────────────────────────────

@dataclass
class HandoffResult:
    """
    Return value from do_handoff(), do_release(), do_claim().
    Always check .ok before using other fields.
    """
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # Populated on success
    previous_state: Optional[Handoff] = None
    new_state: Optional[Handoff] = None

    # Human-readable summary for the CLI to print
    summary: Optional[str] = None


# ─────────────────────────────────────────────────────────────
# INTERNAL HELPERS
# ─────────────────────────────────────────────────────────────

def _load_both(paths: ProjectPaths) -> tuple[Optional[Project], Optional[Handoff], str]:
    """
    Load project.json and handoff.json together.
    Returns (project, handoff, error_message).
    error_message is empty string on success.
    """
    if not paths.project_json.exists():
        return None, None, f"project.json not found at {paths.project_json}"
    if not paths.handoff_json.exists():
        return None, None, f"handoff.json not found at {paths.handoff_json}"

    try:
        project = Project.from_json(paths.project_json.read_text())
    except Exception as e:
        return None, None, f"Could not parse project.json: {e}"

    try:
        handoff = Handoff.from_json(paths.handoff_json.read_text())
    except Exception as e:
        return None, None, f"Could not parse handoff.json: {e}"

    return project, handoff, ""


def _write_handoff(paths: ProjectPaths, handoff: Handoff) -> Optional[str]:
    """
    Write handoff.json atomically.
    Returns error string on failure, None on success.
    """
    try:
        write_json_atomic(paths.handoff_json, handoff.to_json())
        return None
    except Exception as e:
        return f"Could not write handoff.json: {e}"


# ─────────────────────────────────────────────────────────────
# DO HANDOFF
# ─────────────────────────────────────────────────────────────

def do_handoff(
    provider: StorageProvider,
    project_name: str,
    author: str,
    to_identifier: str,
    note: Optional[str] = None,
    force: bool = False,
) -> HandoffResult:
    """
    Pass the ball to a specific collaborator.

    Transition: Any → LOCKED(to_identifier)

    Args:
        provider        storage provider
        project_name    name of the project
        author          identifier of whoever is running this command
        to_identifier   identifier of the recipient collaborator
        note            optional message for the recipient
        force           if True, override an existing lock without error

    Errors (non-force):
        - recipient not in collaborators list
        - project already locked to someone else (use --force to override)

    Warnings (always):
        - handing off to yourself is allowed but unusual
    """
    project_root = provider.project_path(project_name)
    paths = ProjectPaths(project_root)

    project, handoff, load_err = _load_both(paths)
    if load_err:
        return HandoffResult(ok=False, errors=[load_err])

    warnings: list[str] = []

    # ── Recipient must be a known collaborator ─────────────────
    recipient = project.get_collaborator(to_identifier)
    if recipient is None:
        return HandoffResult(
            ok=False,
            errors=[
                f"'{to_identifier}' is not a collaborator on this project. "
                f"Add them first before handing off."
            ],
        )

    # ── Handing off to yourself ────────────────────────────────
    if to_identifier == author:
        warnings.append(
            f"You are handing off to yourself ({to_identifier}). "
            "This locks the project to you."
        )

    # ── Already locked check ───────────────────────────────────
    # Allowed without --force: author holds the lock (normal handoff flow)
    # Requires --force: locked to someone else (override their lock)
    if handoff.lock_state == LockState.LOCKED:
        locked_to = handoff.active_editor or "unknown"
        if locked_to != author:
            if not force:
                return HandoffResult(
                    ok=False,
                    errors=[
                        f"Project is already locked to '{locked_to}'. "
                        f"Use --force to override."
                    ],
                )
            else:
                warnings.append(
                    f"Overriding existing lock (was held by '{locked_to}')."
                )

    # ── Build new handoff state ────────────────────────────────
    new_handoff = Handoff(
        active_editor=to_identifier,
        since=datetime.now(timezone.utc),
        note=note,
        snapshot_index=project.latest_snapshot,
        lock_state=LockState.LOCKED,
    )

    write_err = _write_handoff(paths, new_handoff)
    if write_err:
        return HandoffResult(ok=False, errors=[write_err])

    recipient_name = recipient.display_name
    summary = f"Handed off to {recipient_name} ({to_identifier})"
    if note:
        summary += f'\n  Note: "{note}"'
    if project.latest_snapshot:
        summary += f"\n  At snapshot v{project.latest_snapshot}"

    return HandoffResult(
        ok=True,
        warnings=warnings,
        previous_state=handoff,
        new_state=new_handoff,
        summary=summary,
    )


# ─────────────────────────────────────────────────────────────
# DO RELEASE
# ─────────────────────────────────────────────────────────────

def do_release(
    provider: StorageProvider,
    project_name: str,
    author: str,
    force: bool = False,
) -> HandoffResult:
    """
    Return the project to the idle/open state.

    Transition: LOCKED → OPEN

    Args:
        provider        storage provider
        project_name    name of the project
        author          identifier of whoever is running this command
        force           if True, release even if locked to someone else

    Warnings:
        - already Open (idempotent, no-op)
        - releasing a lock held by someone other than author (force only)

    Errors (non-force):
        - locked to someone other than author
    """
    project_root = provider.project_path(project_name)
    paths = ProjectPaths(project_root)

    project, handoff, load_err = _load_both(paths)
    if load_err:
        return HandoffResult(ok=False, errors=[load_err])

    warnings: list[str] = []

    # ── Already open — idempotent ──────────────────────────────
    if handoff.lock_state == LockState.OPEN:
        return HandoffResult(
            ok=True,
            warnings=["Project is already open (not locked). Nothing to release."],
            previous_state=handoff,
            new_state=handoff,
            summary="Project was already open — no change.",
        )

    # ── Locked to someone else ─────────────────────────────────
    if handoff.active_editor and handoff.active_editor != author:
        if not force:
            return HandoffResult(
                ok=False,
                errors=[
                    f"Project is locked to '{handoff.active_editor}', not you. "
                    f"Use --force to release anyway."
                ],
            )
        else:
            warnings.append(
                f"Releasing a lock held by '{handoff.active_editor}' (--force)."
            )

    # ── Write open state ───────────────────────────────────────
    new_handoff = Handoff(
        active_editor=None,
        since=datetime.now(timezone.utc),
        note=None,
        snapshot_index=project.latest_snapshot,
        lock_state=LockState.OPEN,
    )

    write_err = _write_handoff(paths, new_handoff)
    if write_err:
        return HandoffResult(ok=False, errors=[write_err])

    return HandoffResult(
        ok=True,
        warnings=warnings,
        previous_state=handoff,
        new_state=new_handoff,
        summary="Project released — now open for anyone to claim.",
    )


# ─────────────────────────────────────────────────────────────
# DO CLAIM
# ─────────────────────────────────────────────────────────────

def do_claim(
    provider: StorageProvider,
    project_name: str,
    author: str,
    force: bool = False,
) -> HandoffResult:
    """
    Pick up an idle project — signals to collaborators that you have it.

    Transition: OPEN → LOCKED(author)

    Args:
        provider        storage provider
        project_name    name of the project
        author          identifier of whoever is running this command
        force           if True, claim even if already locked to someone else

    Errors (non-force):
        - project already locked to someone else

    Warnings:
        - project already locked to yourself (idempotent-ish, updates timestamp)
    """
    project_root = provider.project_path(project_name)
    paths = ProjectPaths(project_root)

    project, handoff, load_err = _load_both(paths)
    if load_err:
        return HandoffResult(ok=False, errors=[load_err])

    warnings: list[str] = []

    # ── Author must be a known collaborator ────────────────────
    if project.get_collaborator(author) is None:
        return HandoffResult(
            ok=False,
            errors=[
                f"'{author}' is not a collaborator on this project. "
                f"You cannot claim a project you are not part of."
            ],
        )

    # ── Already locked ─────────────────────────────────────────
    if handoff.lock_state == LockState.LOCKED:
        if handoff.active_editor == author:
            # Already yours — update timestamp, warn, proceed
            warnings.append(
                f"Project is already claimed by you ({author}). "
                "Updating claim timestamp."
            )
        elif not force:
            return HandoffResult(
                ok=False,
                errors=[
                    f"Project is already claimed by '{handoff.active_editor}'. "
                    f"Use --force to override their claim."
                ],
            )
        else:
            warnings.append(
                f"Overriding claim held by '{handoff.active_editor}' (--force)."
            )

    # ── Write locked state ─────────────────────────────────────
    new_handoff = Handoff(
        active_editor=author,
        since=datetime.now(timezone.utc),
        note=None,
        snapshot_index=project.latest_snapshot,
        lock_state=LockState.LOCKED,
    )

    write_err = _write_handoff(paths, new_handoff)
    if write_err:
        return HandoffResult(ok=False, errors=[write_err])

    # Get display name for summary
    collaborator = project.get_collaborator(author)
    display = collaborator.display_name if collaborator else author

    return HandoffResult(
        ok=True,
        warnings=warnings,
        previous_state=handoff,
        new_state=new_handoff,
        summary=f"Project claimed by {display} ({author}).",
    )
