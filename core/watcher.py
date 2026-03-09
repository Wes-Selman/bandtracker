"""
core/watcher.py

FSEvents watcher for BandTracker — Increment 4, updated in Increments 5 & 7.

Monitors the live GarageBand .band bundle for saves (ProjectData changes),
copies the new ProjectData into BandTracker's live/ folder, runs the diff
engine, and prompts the user to save a version.

Increment 5 addition:
  - start() calls reconcile() before the Observer begins, surfacing any
    offline edits made while the watcher was not running.

Increment 7 addition:
  - A second watchdog handler monitors handoff.json for changes written
    by the other machine (or by a local `bandtracker handoff/release/claim`
    command in a different terminal session).
  - On change: silently re-reads handoff.json and updates internal state.
    No terminal output, no OS notification — the state is available via
    watcher.current_handoff for any caller that wants to surface it.
  - The handoff handler uses the same debounce pattern as the ProjectData
    handler to avoid double-firing on atomic writes (.tmp → rename).

Design principles:
  - Nothing in here touches argparse or sys.exit — that's cli/commands/watch.py
  - The prompt callback is injectable for testing (no raw input() calls inside
    the core logic)
  - All path logic goes through ProjectPaths
  - Snapshot taking delegates entirely to core/snapshot.take_snapshot()
  - Watcher can be stopped cleanly from outside (stop()) for test teardown

Terminology:
  "GB bundle"   — the original .band package GarageBand writes to
                  e.g. ~/Music/GarageBand/MidnightDrive.band
  "live bundle" — BandTracker's managed copy in
                  ~/BandTracker/projects/MidnightDrive/live/MidnightDrive.band
  "ProjectData" — the binary file inside the bundle that encodes the session
                  Alternatives/000/ProjectData

Flow on each detected save:
  1. Read new ProjectData from the GB bundle (source of truth after save)
  2. Diff against the current live ProjectData
  3. If changes detected: prompt "Save a version? [y/n]"
  4. y → call take_snapshot() with auto-generated or user-provided description
  5. n → update live ProjectData silently (keep live/ in sync without snapping)
  6. Loop — watchdog keeps watching

Flow on handoff.json change (Increment 7):
  1. Re-read handoff.json from disk
  2. Update self.current_handoff silently
  3. No terminal output, no prompt
"""

from __future__ import annotations

import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from core.models import Handoff, Project, ProjectPaths, StorageProvider
from core.diff.engine import byte_diff, build_description
from core.diff.noise import load_noise_mask
from core.diff.interpreter import interpret_changes
from core.snapshot import take_snapshot, SnapshotResult
from core.reconcile import reconcile


# ─────────────────────────────────────────────────────────────
# TYPES
# ─────────────────────────────────────────────────────────────

# Signature: (prompt: str) -> str
# Injected so tests can answer without stdin
PromptFn = Callable[[str], str]

# Signature: (message: str) -> None
# Injected so tests can capture output without print()
PrintFn = Callable[[str], None]


# ─────────────────────────────────────────────────────────────
# RESULT
# ─────────────────────────────────────────────────────────────

@dataclass
class WatchEvent:
    """
    Record of a single detected save and what happened next.
    Collected by the watcher for testing and logging.
    """
    gb_project_data_path: Path
    diff_summary: list[str] = field(default_factory=list)
    description: Optional[str] = None
    user_chose_snapshot: Optional[bool] = None   # None if no prompt shown
    snapshot_result: Optional[SnapshotResult] = None
    error: Optional[str] = None


# ─────────────────────────────────────────────────────────────
# INTERNAL: DIFF HELPERS
# ─────────────────────────────────────────────────────────────

def _run_diff(
    old_bytes: bytes,
    new_bytes: bytes,
    noise_mask_path: Optional[Path],
) -> tuple[list[str], Optional[str]]:
    """
    Compare old_bytes and new_bytes through the full diff pipeline.

    Returns:
        diff_summary    list of human-readable change lines (may be empty)
        description     single auto-generated description string, or None
    """
    try:
        noise_mask = load_noise_mask(noise_mask_path) if noise_mask_path and noise_mask_path.exists() else None

        result = byte_diff(old_bytes, new_bytes, noise_mask=noise_mask)
        if not result.ok:
            return [], None

        interpreted = interpret_changes(result, full_changed_bytes=new_bytes)
        description = build_description(result, interpreted)

        summary = interpreted if interpreted else []
        desc = description if (description and description != "no changes detected") else None
        return summary, desc

    except Exception:
        return [], None


# ─────────────────────────────────────────────────────────────
# INTERNAL: SYNC LIVE ProjectData
# ─────────────────────────────────────────────────────────────

def _sync_live_project_data(
    gb_project_data: Path,
    live_project_data: Path,
) -> Optional[str]:
    """
    Copy the GB bundle's ProjectData into BandTracker's live/ folder.
    This keeps live/ in sync even when the user says "no" to snapshotting.

    Returns an error string on failure, or None on success.
    """
    try:
        live_project_data.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(gb_project_data, live_project_data)
        return None
    except Exception as e:
        return f"Failed to sync ProjectData: {e}"


# ─────────────────────────────────────────────────────────────
# WATCHDOG EVENT HANDLER — ProjectData
# ─────────────────────────────────────────────────────────────

class _ProjectDataHandler(FileSystemEventHandler):
    """
    Watchdog handler that fires whenever a file inside the watched
    GB bundle directory changes.

    We only care about writes to ProjectData specifically. GarageBand
    writes ProjectData atomically (write new → rename), so we watch
    for both modified and created events on the target path.

    A debounce lock prevents double-firing on atomic rename sequences
    (GarageBand often writes temp file then renames it).
    """

    DEBOUNCE_SECONDS = 1.5

    def __init__(
        self,
        gb_project_data: Path,
        on_save: Callable[[Path], None],
    ):
        super().__init__()
        self._gb_project_data = gb_project_data
        self._on_save = on_save
        self._lock = threading.Lock()
        self._last_fired: float = 0.0

    def _is_target(self, path_str: str) -> bool:
        try:
            return Path(path_str).resolve() == self._gb_project_data.resolve()
        except Exception:
            return False

    def _maybe_fire(self, path_str: str) -> None:
        if not self._is_target(path_str):
            return
        now = time.monotonic()
        with self._lock:
            if now - self._last_fired < self.DEBOUNCE_SECONDS:
                return
            self._last_fired = now
        # Fire outside the lock so on_save() can be slow
        self._on_save(self._gb_project_data)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._maybe_fire(event.src_path)

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._maybe_fire(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        # Atomic rename: dest is the final path
        if not event.is_directory:
            self._maybe_fire(event.dest_path)


# ─────────────────────────────────────────────────────────────
# WATCHDOG EVENT HANDLER — handoff.json (Increment 7)
# ─────────────────────────────────────────────────────────────

class _HandoffHandler(FileSystemEventHandler):
    """
    Watches the project root directory for changes to handoff.json.

    On any write to handoff.json (including atomic .tmp → rename writes
    from `bandtracker handoff/release/claim`), silently re-reads the
    file and calls on_handoff_change() with the new Handoff state.

    Uses the same debounce pattern as _ProjectDataHandler to avoid
    double-firing on atomic rename sequences.

    Silent by design — no terminal output from within this handler.
    The caller (ProjectWatcher) decides what to do with the new state.
    """

    DEBOUNCE_SECONDS = 1.0

    def __init__(
        self,
        handoff_json_path: Path,
        on_handoff_change: Callable[[Handoff], None],
    ):
        super().__init__()
        self._handoff_path = handoff_json_path
        self._on_handoff_change = on_handoff_change
        self._lock = threading.Lock()
        self._last_fired: float = 0.0

    def _is_target(self, path_str: str) -> bool:
        try:
            return Path(path_str).resolve() == self._handoff_path.resolve()
        except Exception:
            return False

    def _maybe_fire(self, path_str: str) -> None:
        if not self._is_target(path_str):
            return
        now = time.monotonic()
        with self._lock:
            if now - self._last_fired < self.DEBOUNCE_SECONDS:
                return
            self._last_fired = now
        # Read and parse outside the lock — I/O can be slow
        try:
            text = self._handoff_path.read_text(encoding="utf-8")
            new_handoff = Handoff.from_json(text)
            self._on_handoff_change(new_handoff)
        except Exception:
            # Corrupt or mid-write file — ignore, next write will retry
            pass

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._maybe_fire(event.src_path)

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._maybe_fire(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._maybe_fire(event.dest_path)


# ─────────────────────────────────────────────────────────────
# WATCHER
# ─────────────────────────────────────────────────────────────

class ProjectWatcher:
    """
    Watches a GarageBand .band bundle for saves and offers to snapshot.
    Also silently watches handoff.json for lock state changes from
    the other machine (Increment 7).

    Usage:
        watcher = ProjectWatcher(provider, project_name, author)
        watcher.start()    # starts background observer thread
        watcher.join()     # blocks until stop() called elsewhere
        watcher.stop()     # signal shutdown

    Or use as a context manager:
        with ProjectWatcher(...) as w:
            w.join()

    Args:
        provider        storage provider (knows BandTracker root)
        project_name    name of the managed project
        author          identifier of the current user
        gb_band_path    path to the ORIGINAL .band file GarageBand writes to
                        (not the managed copy in live/)
        prompt_fn       callable(str) -> str for user input (default: input())
        print_fn        callable(str) -> None for output (default: print())
        auto_yes        if True, always answer "y" without prompting
                        (useful for --auto flag or testing)

    Attributes:
        current_handoff     most recently observed Handoff state.
                            Updated silently whenever handoff.json changes.
                            None until start() is called.
        handoff_changes     list of Handoff states observed since start(),
                            in order. Useful for testing.
    """

    def __init__(
        self,
        provider: StorageProvider,
        project_name: str,
        author: str,
        gb_band_path: Path,
        prompt_fn: Optional[PromptFn] = None,
        print_fn: Optional[PrintFn] = None,
        auto_yes: bool = False,
    ):
        self.provider = provider
        self.project_name = project_name
        self.author = author
        self.gb_band_path = gb_band_path
        self.auto_yes = auto_yes

        self._prompt_fn: PromptFn = prompt_fn or input
        self._print_fn: PrintFn = print_fn or print

        project_root = provider.project_path(project_name)
        self._paths = ProjectPaths(project_root)

        # ProjectData paths
        self._gb_pd = (
            gb_band_path / "Alternatives" / "000" / "ProjectData"
        )
        self._live_pd = self._paths.live_project_data(project_name)

        # Internal state
        self._observer: Optional[Observer] = None
        self._stop_event = threading.Event()
        self._event_lock = threading.Lock()  # serialize on_save calls

        # Handoff state — updated silently by _HandoffHandler (Increment 7)
        self.current_handoff: Optional[Handoff] = None
        self._handoff_lock = threading.Lock()
        self.handoff_changes: list[Handoff] = []

        # Collected for testing / logging
        self.events: list[WatchEvent] = []

    # ── Lifecycle ────────────────────────────────────────────

    def start(self) -> None:
        """
        Start the watchdog observer in a background thread.

        Reconciliation runs first (synchronously) before the Observer
        begins. If offline edits are detected, the musician is prompted
        to snapshot them before watching begins. This ensures the watcher
        always starts from a clean known baseline.

        The initial handoff state is loaded from disk before the observer
        starts so current_handoff is never None after start() returns.
        """
        if self._observer is not None:
            raise RuntimeError("Watcher already started")

        # ── Load initial handoff state ─────────────────────────
        try:
            text = self._paths.handoff_json.read_text(encoding="utf-8")
            self.current_handoff = Handoff.from_json(text)
        except Exception:
            self.current_handoff = None

        # ── Reconcile before watching ──────────────────────────
        reconcile(
            provider=self.provider,
            project_name=self.project_name,
            author=self.author,
            gb_band_path=self.gb_band_path,
            prompt_fn=self._prompt_fn,
            print_fn=self._print_fn,
        )

        # ── Start Observer ─────────────────────────────────────
        # One Observer, two schedules:
        #   - ProjectData changes inside the GB bundle directory
        #   - handoff.json changes in the project root directory

        self._observer = Observer()

        # Schedule 1: GB bundle directory → ProjectData changes
        pd_handler = _ProjectDataHandler(
            gb_project_data=self._gb_pd,
            on_save=self._on_save,
        )
        self._observer.schedule(pd_handler, str(self._gb_pd.parent), recursive=False)

        # Schedule 2: project root → handoff.json changes
        handoff_handler = _HandoffHandler(
            handoff_json_path=self._paths.handoff_json,
            on_handoff_change=self._on_handoff_change,
        )
        self._observer.schedule(
            handoff_handler,
            str(self._paths.project_root),
            recursive=False,
        )

        self._observer.start()
        self._print_fn(
            f"Watching {self.gb_band_path.name} — "
            f"press Ctrl+C to stop"
        )

    def stop(self) -> None:
        """Signal the watcher to stop."""
        self._stop_event.set()
        if self._observer is not None:
            self._observer.stop()

    def join(self) -> None:
        """Block until stop() is called."""
        try:
            while not self._stop_event.is_set():
                time.sleep(0.25)
        finally:
            if self._observer is not None:
                self._observer.join()

    def __enter__(self) -> ProjectWatcher:
        self.start()
        return self

    def __exit__(self, *_) -> None:
        self.stop()

    # ── Handoff state update (Increment 7) ───────────────────

    def _on_handoff_change(self, new_handoff: Handoff) -> None:
        """
        Called silently by _HandoffHandler when handoff.json changes.
        Updates current_handoff and appends to handoff_changes log.
        No terminal output — silent by design.
        """
        with self._handoff_lock:
            self.current_handoff = new_handoff
            self.handoff_changes.append(new_handoff)

    # ── Core save handler ─────────────────────────────────────

    def _on_save(self, gb_pd_path: Path) -> None:
        """
        Called by the watchdog handler on each debounced ProjectData write.
        Serialized via _event_lock so two rapid saves don't race.
        """
        with self._event_lock:
            event = WatchEvent(gb_project_data_path=gb_pd_path)
            self.events.append(event)

            # 1. Read new ProjectData from GB
            try:
                new_bytes = gb_pd_path.read_bytes()
            except Exception as e:
                event.error = f"Could not read ProjectData: {e}"
                self._print_fn(f"[bandtracker] Warning: {event.error}")
                return

            # 2. Read current live ProjectData (baseline for diff)
            try:
                old_bytes = self._live_pd.read_bytes() if self._live_pd.exists() else b""
            except Exception:
                old_bytes = b""

            # 3. Diff
            diff_summary, description = _run_diff(
                old_bytes,
                new_bytes,
                self._paths.noise_mask_json,
            )
            event.diff_summary = diff_summary
            event.description = description

            # 4. Announce the save
            self._print_fn("\n── GarageBand save detected ─────────────────────")
            if description:
                self._print_fn(f"Changes: {description}")
            elif diff_summary:
                for line in diff_summary:
                    self._print_fn(f"  • {line}")
            else:
                self._print_fn("  (no structured changes detected)")

            # 5. Prompt
            if self.auto_yes:
                answer = "y"
            else:
                try:
                    answer = self._prompt_fn("Save a version? [y/n] ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    answer = "n"

            event.user_chose_snapshot = answer == "y"

            if answer == "y":
                # 5a. Sync live/ first so take_snapshot() sees the new bytes
                sync_err = _sync_live_project_data(gb_pd_path, self._live_pd)
                if sync_err:
                    event.error = sync_err
                    self._print_fn(f"[bandtracker] Error: {sync_err}")
                    return

                # 5b. Ask for optional message
                if self.auto_yes:
                    msg = description  # use auto-generated
                else:
                    try:
                        raw = self._prompt_fn(
                            "Description (leave blank to auto-generate): "
                        ).strip()
                        msg = raw if raw else description
                    except (EOFError, KeyboardInterrupt):
                        msg = description

                result = take_snapshot(
                    provider=self.provider,
                    project_name=self.project_name,
                    author=self.author,
                    message=msg or None,
                )
                event.snapshot_result = result

                if result.ok:
                    self._print_fn(
                        f"✓ Snapshot {result.snapshot_index} saved"
                        + (f': "{result.description}"' if result.description else "")
                    )
                else:
                    self._print_fn(
                        f"✗ Snapshot failed: {'; '.join(result.errors)}"
                    )

            else:
                # 5b. Still sync live/ so next diff has correct baseline
                sync_err = _sync_live_project_data(gb_pd_path, self._live_pd)
                if sync_err:
                    self._print_fn(f"[bandtracker] Warning: {sync_err}")
                self._print_fn("Skipped.")

            self._print_fn("─" * 50)


# ─────────────────────────────────────────────────────────────
# PREFLIGHT
# ─────────────────────────────────────────────────────────────

@dataclass
class PreflightResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    gb_pd_path: Optional[Path] = None


def preflight(
    provider: StorageProvider,
    project_name: str,
    gb_band_path: Path,
) -> PreflightResult:
    """
    Validate everything before starting the watcher.

    Checks:
      - BandTracker project exists (project.json present)
      - GB .band bundle exists at gb_band_path
      - ProjectData exists inside the GB bundle
      - The managed live/ bundle exists (init was run)
    """
    project_root = provider.project_path(project_name)
    paths = ProjectPaths(project_root)
    errors: list[str] = []
    warnings: list[str] = []

    if not paths.project_json.exists():
        errors.append(
            f"No BandTracker project found for '{project_name}'. "
            f"Run `bandtracker init` first."
        )

    if not gb_band_path.exists():
        errors.append(f"GarageBand bundle not found: {gb_band_path}")

    gb_pd = gb_band_path / "Alternatives" / "000" / "ProjectData"
    if gb_band_path.exists() and not gb_pd.exists():
        errors.append(f"ProjectData not found inside bundle: {gb_pd}")

    live_band = paths.live_band(project_name)
    if not live_band.exists():
        errors.append(
            f"Managed live bundle missing: {live_band}. "
            f"Run `bandtracker init` first."
        )

    if errors:
        return PreflightResult(ok=False, errors=errors, warnings=warnings)

    return PreflightResult(
        ok=True,
        warnings=warnings,
        gb_pd_path=gb_pd,
    )
