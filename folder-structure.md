# BandTracker — On-Disk Folder Structure

This document is the canonical reference for how BandTracker organizes
projects on disk. **Do not change this structure without updating this
document and incrementing the schema version in project.json.**

## Root Layout

```
~/BandTracker/                          ← configurable root (StorageProvider.root_path)
  projects/
    {ProjectName}/                      ← human-readable folder name, matches project.name
      project.json                      ← Project metadata and collaborators
      handoff.json                      ← Live coordination: who has the ball
      noise_mask.json                   ← Learned noise offsets for this project
      live/
        {ProjectName}.band/             ← The managed .band bundle GarageBand works with
          Alternatives/
            000/
              ProjectData               ← GarageBand's binary project state (magic: gnoS at offset 24)
          Media/
            Audio Files/
              Guitar Take 1.aif         ← Audio files GarageBand creates
              Vocal Take 1.aif
      media/                            ← Deduplicated audio store (content-addressed)
        {sha256}.aif                    ← Each unique audio file stored once by hash
        {sha256}.caf
      snapshots/
        001/                            ← Zero-padded snapshot index
          ProjectData                   ← Copy of binary state at snapshot time (~2–5MB)
          meta.json                     ← Snapshot metadata (see schema below)
          manifest.json                 ← Maps original filenames → content hashes
          sidecar/                      ← Files attached to this snapshot
            bounce.m4a                  ← version-type: pinned to this snapshot only
            lyrics.txt                  ← project-type: inherits forward across snapshots
            notes.md
        002/
          ...
```

## File Schemas

### project.json
```json
{
  "name": "Midnight Drive",
  "uuid": "a3f7c2d1-...",
  "created_at": "2024-03-08T12:00:00+00:00",
  "owner": "jordan@email.com",
  "collaborators": [
    { "display_name": "Jordan", "identifier": "jordan@email.com" },
    { "display_name": "Maya",   "identifier": "maya@email.com" }
  ],
  "garageband_version": "10.4.8",
  "latest_snapshot": 7,
  "next_snapshot_index": 8,
  "gb_bundle_path": "~/Music/GarageBand/Midnight Drive.band",
  "gb_bundle_alias": "<base64-encoded macOS NSURL bookmark>"
}
```

`gb_bundle_path` — path to the GarageBand bundle GB saves to, stored as a
`~/...` string for cross-machine readability. `None` for projects initialized
before Increment 5 — run `bandtracker set-gb` to populate.

`gb_bundle_alias` — macOS NSURL bookmark enabling silent resolution if the
file is moved within the same volume. `None` on non-macOS or when PyObjC
is unavailable.

### handoff.json
```json
{
  "active_editor": "maya@email.com",
  "since": "2024-03-08T14:23:00+00:00",
  "note": "Verse and chorus solid. Bridge needs work. Leave drums alone.",
  "snapshot_index": 7,
  "lock_state": "locked"
}
```

`lock_state` is either `"open"` (no handoff in progress) or `"locked"`.
Written atomically (write temp file, rename) to prevent partial reads.
`active_editor` is `null` when `lock_state` is `"open"`.

### snapshots/{n}/meta.json
```json
{
  "index": 7,
  "description": "Added harmony vocals on bridge",
  "timestamp": "2024-03-08T16:45:00+00:00",
  "author": "maya@email.com",
  "diff_summary": ["structural changes detected (1 track added)", "pan changed (+12 → 0, center)"],
  "milestone": "handoff",
  "media": [
    { "original_name": "Guitar Take 1.aif", "content_hash": "a3f7c2...", "size_bytes": 157286400 },
    { "original_name": "Vocal Take 1.aif",  "content_hash": "b9e4d1...", "size_bytes": 83886080 }
  ],
  "sidecar_files": [
    { "filename": "bounce.m4a", "type": "version" },
    { "filename": "lyrics.txt", "type": "project" }
  ]
}
```

`milestone` is one of: `null`, `"arrangement_lock"`, `"final_mix"`, `"handoff"`.

`sidecar_files` entries carry a `type` field:
- `"version"` — pinned to this snapshot only. Not visible from other snapshots.
- `"project"` — living document. Inherits forward; the most recent copy of the
  same filename across snapshots ≤ N wins (shadowing).

Backward compatibility: pre-Increment-8 `meta.json` files stored `sidecar_files`
as a plain list of strings. These are read back as `type: "version"` automatically.

### snapshots/{n}/manifest.json
```json
[
  { "original_name": "Guitar Take 1.aif", "content_hash": "a3f7c2...", "size_bytes": 157286400 },
  { "original_name": "Vocal Take 1.aif",  "content_hash": "b9e4d1...", "size_bytes": 83886080 }
]
```

### noise_mask.json
```json
{
  "learned_at": "2024-03-08T12:05:00+00:00",
  "rounds": 3,
  "noisy_offsets": [18, 19, 20, 42, "..."]
}
```

## Design Principles

**Content-addressed media store.** Audio files in `media/` are stored by
SHA-256 hash of their content, not by name. GarageBand never modifies audio
files after creating them — they are immutable. Each unique file is stored
exactly once regardless of how many snapshots reference it.

**ProjectData is the only per-snapshot cost.** At 2–5MB per snapshot, 20
snapshots adds ~100MB — negligible compared to audio. Audio accumulates in
`media/` but is never duplicated.

**Storage independence.** The root path (`~/BandTracker`) is configurable
via `StorageProvider` and `ProjectPaths` — no path logic is hardcoded outside
of these classes. The infrastructure for pointing BandTracker at a shared folder
(Dropbox, iCloud) is in place, but multi-machine collaboration over a shared root
has not been tested end to end. Real two-machine sync is a post-Increment-14
validation task.

**Atomic writes for coordination files.** `handoff.json` and all JSON files
are always written by: write to `{file}.tmp`, then `rename()` to the target.
`rename()` is atomic on POSIX systems. Prevents partial reads during sync.

**Names are display, UUIDs are identity.** The project folder is named after
the song. The `uuid` field in `project.json` is the stable internal identity
that survives renames. UUIDs are never shown to musicians.

**Sidecar inheritance over a docs/ folder.** The `docs/` directory exists in
the folder structure but is not actively used. Project-level living documents
(lyrics, chord charts, producer notes) are handled by sidecar attachments with
`type: "project"`, which provides version history and inheritance. This is more
powerful than a flat docs/ folder because the document's evolution is tied to
the snapshot timeline.

**`identifier` is always opaque.** Collaborator identifiers look like email
addresses but are never parsed as such. They are stable strings both machines
agree on — a future auth migration can replace them with tokens or UUIDs without
changing any business logic.
