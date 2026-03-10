# BandTracker

Version control for GarageBand.

Every save, every decision, every handoff — tracked automatically and described in plain language.

## What it does

- **Watches** your GarageBand project for saves
- **Describes** what changed in plain English: *"tempo changed to 124 BPM, 2 tracks added"*
- **Snapshots** meaningful versions with a name and timestamp
- **Restores** any previous version in one command
- **Hands off** a project to a collaborator with a note, and detects if you both edit at once
- **Attaches** notes, lyrics, and bounces to specific versions — with inheritance so living documents follow the project forward

## What it doesn't do

In the current iteration, files are only stored locally and while commands exist for collaborative flows they are not tested across machines.

## Architecture

```
core/               Python — all business logic, no I/O assumptions
  models.py             data contracts (Project, Snapshot, Handoff, SidecarEntry, ...)
  init.py               project initialization, atomic writes, path validation
  snapshot.py           snapshot writer + media deduplication
  restore.py            safe rollback
  watcher.py            FSEvents file watcher (foreground today, daemon in Increment 12)
  reconcile.py          launch-time check for offline edits
  handoff_ops.py        soft lock + conflict detection
  sidecar.py            notes, lyrics, bounce attachments with inheritance
  bundle_ref.py         macOS NSURL bookmark storage for .band bundle path
  diff/
    engine.py           binary diff against ProjectData
    noise.py            noise mask (filters GarageBand's spurious save noise)
    interpreter.py      byte changes → human-readable descriptions

cli/                Python — command-line interface (Phases 1–2)
  main.py               command router
  commands/             one file per subcommand
    init.py             bandtracker init
    snapshot.py         bandtracker snapshot
    restore.py          bandtracker restore
    watch.py            bandtracker watch
    reconcile.py        bandtracker reconcile
    handoff.py          bandtracker handoff
    claim.py            bandtracker claim
    release.py          bandtracker release
    attach.py           bandtracker attach
    detach.py           bandtracker detach
    attachments.py      bandtracker attachments
    learn_noise.py      bandtracker learn-noise
    set_gb.py           bandtracker set-gb

app/                Swift — macOS menu bar + full window app (Phase 3, Increment 13+)

tests/              pytest test suite (442 passing, 4 skipped)
docs/               design documents and delivery plan
```

## Storage

Projects live in `~/BandTracker/projects/{ProjectName}/`.
The root path is configurable via `StorageProvider` and `ProjectPaths` — the storage
abstraction is in place but multi-machine collaboration over a shared folder (Dropbox,
iCloud) has not yet been tested end to end. The handoff lock mechanism works locally;
real two-machine sync is a post-Increment-14 validation task.

See [docs/folder-structure.md](docs/folder-structure.md) for the full on-disk layout.

## Delivery plan

See [docs/increments.md](docs/increments.md) for the full incremental delivery plan.

**Current status: Increment 8 complete.**
CLI foundation is fully functional. Next: Increment 9 — project management commands.

## Running tests

```bash
pip install -e ".[dev]"
pytest tests/test_models.py -v     # models only
pytest                             # full suite
```

## Background

BandTracker builds on [band-cartographer](https://github.com/Wes-Selman/band-cartographer),
a collaborative reverse engineering project that mapped GarageBand's undocumented binary format.

Key findings from that research:
- `ProjectData` contains magic bytes `gnoS` at offset 24
- A no-op save produces ~18,600 spurious changed byte ranges (filtered by noise mask)
- Confirmed decodeable fields: pan, mute, volume, tempo (µs/beat as uint32), time signature
- Structural changes (add track, add region) detectable by large byte insertions

The diff engine in `core/diff/` is a production port of that research.
band-cartographer continues as a separate research repo — findings are ported
into BandTracker's interpreter manually as new fields are mapped.
A future increment will evaluate extracting band-cartographer as an installable
package once interpreter coverage is broad enough to warrant versioning.
