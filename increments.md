# BandTracker — Incremental Delivery Plan

Each increment is independently useful and testable.
Pick up from any increment after a break — the status column tells you where things stand.

| # | Name | Status | Delivers |
|---|------|--------|----------|
| 0 | Repo structure + models | ✅ Complete | Folder structure, data contracts, test fixtures |
| 1 | Project initialization | ⬜ Not started | `bandtracker init` — move .band into managed storage, snapshot 001 |
| 2 | Snapshot writer | ⬜ Not started | `bandtracker snapshot` — deduplicated media, manifest, meta |
| 3 | Diff engine | ⬜ Not started | `bandtracker diff` — port from band_cartographer, human-readable summaries |
| 4 | FSEvents watcher | ⬜ Not started | `bandtracker watch` — detect saves, prompt for snapshot |
| 5 | Reconciliation | ⬜ Not started | Launch check for offline edits |
| 6 | Restore | ⬜ Not started | `bandtracker restore <n>` — safe rollback |
| 7 | Handoff | ⬜ Not started | `bandtracker handoff` — soft lock, conflict detection |
| 8 | Sidecar documents | ⬜ Not started | `bandtracker attach` — notes, lyrics, bounces |

## Increment 0 — Repo structure + models ✅

**Deliverables:**
- Full folder structure established
- `core/models.py` — all dataclasses with JSON serialization
- `core/diff/` — stub files with documented responsibilities
- `cli/main.py` — command router
- `tests/test_models.py` — full model test coverage
- `tests/fixtures/minimal.band/` — synthetic .band bundle for testing
- `docs/folder-structure.md` — canonical folder reference

**How to verify:**
```bash
pytest tests/test_models.py -v
```
All tests should pass. No I/O, no GarageBand required.

---

## Increment 1 — Project initialization

**Goal:** Run `bandtracker init ~/Music/GarageBand/MidnightDrive.band`
and have a fully initialized project in `~/BandTracker/projects/MidnightDrive/`.

**Deliverables:**
- `core/init.py` — full implementation
- `cli/commands/init.py` — CLI handler
- `tests/test_init.py` — full test coverage

**Key behaviors to implement:**
1. Validate the .band bundle (exists, has Output/ProjectData, magic bytes gnoS)
2. Sanitize project name from bundle filename (strip .band, handle special chars)
3. Check for name collision in projects/ — prompt to rename if exists
4. Create full folder structure (live/, media/, snapshots/, docs/)
5. Copy .band bundle into live/ (copy then verify, never move-then-fail)
6. Hash and copy existing media files into media/
7. Write project.json (Project.create()) and handoff.json (Handoff.open())
8. Take snapshot 001 with description "Initial version", no diff_summary
9. Print confirmation with project path and snapshot count

**Key failure modes to handle:**
- .band bundle not found or invalid
- Insufficient disk space (check before copying)
- GarageBand has the file open (check for lock file)
- project name already exists in projects/

**How to verify:**
```bash
bandtracker init ~/Music/GarageBand/MidnightDrive.band
# Then inspect:
cat ~/BandTracker/projects/MidnightDrive/project.json
cat ~/BandTracker/projects/MidnightDrive/snapshots/001/meta.json
ls ~/BandTracker/projects/MidnightDrive/media/
```

---

## Increment 2 — Snapshot writer

**Goal:** Run `bandtracker snapshot -m "Verse structure done"` and have a
new snapshot appear in the timeline with correct manifest and deduplicated media.

**Deliverables:**
- `core/snapshot.py` — full implementation
- `cli/commands/snapshot.py` — CLI handler
- `tests/test_snapshot.py` — full test coverage

**Key behaviors:**
1. Ensure all current media files are in media/ before writing manifest
2. Write ProjectData copy, manifest.json, meta.json atomically
3. Update project.json (latest_snapshot, next_snapshot_index)
4. Deduplication: skip media/ copy if content_hash already exists
5. Support milestone tags (--milestone arrangement_lock etc.)
6. If no --message, use auto-generated description (placeholder until Increment 3)

---

## Increment 3 — Diff engine

**Goal:** `bandtracker snapshot` auto-generates a human-readable description
of what changed since the last snapshot. `bandtracker diff <n>` shows the
diff between any two snapshots.

**Deliverables:**
- `core/diff/engine.py` — ported from band_cartographer.py
- `core/diff/noise.py` — ported from band_cartographer.py
- `core/diff/interpreter.py` — ported from band_cartographer.py
- `cli/commands/learn_noise.py` — interactive noise learning
- `tests/test_diff.py` — full test coverage (port from test_bandtracker.py)

**Port checklist from band_cartographer.py:**
- [ ] byte_diff() → engine.py
- [ ] apply_noise_mask() → noise.py
- [ ] load_noise_mask() → noise.py
- [ ] _find_tempo_offsets() → engine.py
- [ ] _decode_tempo() → interpreter.py
- [ ] interpret_changes() → interpreter.py
- [ ] build_commit_message() → engine.py as build_description()

---

## Increment 4 — FSEvents watcher

**Goal:** `bandtracker watch` runs in the foreground, detects GarageBand
saves, diffs against last snapshot, and prompts "Save a version? [y/n]"
in the terminal.

**Deliverables:**
- `core/watcher.py` — full implementation
- `cli/commands/watch.py` — CLI handler
- `tests/test_watcher.py` — tests using mock FSEvents

**Platform note:** Use `watchdog` library for cross-platform testing.
On macOS production, use FSEvents directly via watchdog's macOS backend.

---

## Increment 5 — Reconciliation

**Goal:** When BandTracker launches and a project's live/ ProjectData
differs from the last snapshot, surface this clearly and prompt to snapshot
before starting the watcher.

---

## Increment 6 — Restore

**Goal:** `bandtracker restore 3` restores the project to snapshot 3.
GarageBand must be closed. Adds a new snapshot tagged with the restore event.

---

## Increment 7 — Handoff

**Goal:** `bandtracker handoff --to maya@email.com --note "Bridge needs work"`
writes handoff.json and updates the lock state. The other machine detects
the change via FSEvents on handoff.json and surfaces a notification.

---

## Increment 8 — Sidecar documents

**Goal:** `bandtracker attach notes.md` attaches a file to the latest
snapshot. `bandtracker attach bounce.m4a --snapshot 7` attaches to a
specific version.
