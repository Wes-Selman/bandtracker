# BandTracker

Version control for GarageBand. Built for musicians, not developers.

Every save, every decision, every handoff — tracked automatically and described in plain language.

## What it does

- **Watches** your GarageBand project for saves
- **Describes** what changed in plain English: *"tempo changed to 124 BPM, 2 tracks added"*
- **Snapshots** meaningful versions with a name and timestamp
- **Restores** any previous version in one command
- **Hands off** a project to a collaborator with a note, and detects if you both edit at once
- **Attaches** notes, lyrics, and bounces to specific versions

## What it doesn't do

Use GitHub, require a server, touch the cloud, or ask you to understand version control.

## Architecture

```
core/       Python — all business logic
  models.py         data contracts (Project, Snapshot, Handoff, ...)
  init.py           project initialization
  snapshot.py       snapshot writer + media deduplication
  restore.py        safe rollback
  watcher.py        FSEvents file watcher
  reconciler.py     launch-time check for offline edits
  handoff.py        soft lock + conflict detection
  sidecar.py        notes, lyrics, bounce attachments
  diff/
    engine.py       binary diff against ProjectData
    noise.py        noise mask (filters GarageBand's spurious saves)
    interpreter.py  byte changes → human-readable descriptions

cli/        Python — command-line interface (Phase 1)
app/        Swift — macOS menu bar app (Phase 2, not yet built)
tests/      pytest test suite
docs/       Design documents
```

## Storage

Projects live in `~/BandTracker/projects/{ProjectName}/`.
The root folder is configurable — point it at a Dropbox or iCloud folder
to sync with a collaborator. BandTracker doesn't care where the folder is.

See [docs/folder-structure.md](docs/folder-structure.md) for the full on-disk layout.

## Build sequence

See [docs/increments.md](docs/increments.md) for the incremental delivery plan.
Each increment is independently testable. Pick up from any point.

**Current status: Increment 0 complete** — models and structure established.
Next: Increment 1 — project initialization.

## Running tests

```bash
pip install pytest
pytest tests/test_models.py -v    # Increment 0 — runs now
pytest                            # Full suite — some tests pending implementation
```

## Background

BandTracker builds on [band-cartographer](https://github.com/Wes-Selman/band-cartographer), a collaborative reverse engineering project that mapped GarageBand's undocumented binary format.

Key findings from that research:
- `ProjectData` starts with magic bytes `gnoS`
- A no-op save produces ~18,600 spurious changed byte ranges (filtered by noise mask)
- Confirmed decodeable fields: pan, mute, volume, tempo (µs/beat as uint32), time signature
- Structural changes (add track, add region) detectable by large byte insertions

If you're interested in the binary format research itself, start there.
This project builds the collaboration layer on top of it.
