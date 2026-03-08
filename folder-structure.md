# BandTracker — On-Disk Folder Structure

This document is the canonical reference for how BandTracker organizes
projects on disk. **Do not change this structure without updating this
document and incrementing the schema version in project.json.**

## Root Layout

```
~/BandTracker/                          ← configurable root (StorageProvider.root_path)
  projects/
    {ProjectName}/                      ← human-readable folder name, matches project.name
      project.json                      ← Project metadata and identity
      handoff.json                      ← Live coordination: who has the ball
      noise_mask.json                   ← Learned noise offsets for this project
      live/
        {ProjectName}.band/             ← The managed .band bundle GarageBand works with
          Output/
            ProjectData                 ← GarageBand's binary project state (magic: gnoS)
          Media/
            Audio Files/
              Guitar Take 1.aif         ← Audio files GarageBand creates
              Vocal Take 1.aif
      media/                            ← Deduplicated audio store (content-addressed)
        {sha256}.aif                    ← Each unique audio file stored once by hash
        {sha256}.caf
      snapshots/
        001/                            ← Zero-padded snapshot index
          ProjectData                   ← Copy of binary at snapshot time (~2-5MB)
          meta.json                     ← Snapshot metadata (see below)
          manifest.json                 ← Maps original filenames → content hashes
          sidecar/                      ← Files attached to this specific version
            bounce.m4a                  ← Optional audio export
            notes.md                    ← Jordan's note to Maya
            lyrics.txt
        002/
          ...
      docs/                             ← Project-level documents (not version-specific)
        chord-chart.pdf
        arrangement-notes.md
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
  "next_snapshot_index": 8
}
```

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

### snapshots/{n}/meta.json
```json
{
  "index": 7,
  "description": "Added harmony vocals on bridge",
  "timestamp": "2024-03-08T16:45:00+00:00",
  "author": "maya@email.com",
  "diff_summary": ["structural changes detected (1 track added)", "pan changed (+12 → 0, center)"],
  "milestone": "handoff",
  "sidecar_files": ["bounce.m4a", "notes.md"]
}
```
`milestone` is one of: `null`, `"arrangement_lock"`, `"final_mix"`, `"handoff"`.

### snapshots/{n}/manifest.json
```json
{
  "entries": [
    { "original_name": "Guitar Take 1.aif", "content_hash": "a3f7c2...", "size_bytes": 157286400 },
    { "original_name": "Vocal Take 1.aif",  "content_hash": "b9e4d1...", "size_bytes": 83886080  }
  ]
}
```

### noise_mask.json
```json
{
  "learned_at": "2024-03-08T12:05:00+00:00",
  "rounds": 3,
  "noisy_offsets": [18, 19, 20, 42, ...]
}
```

## Design Principles

**Content-addressed media store.** Audio files in `media/` are stored by
SHA-256 hash of their content, not by name. GarageBand never modifies
audio files after creating them — they are immutable. Storing by hash
means each unique file is stored exactly once regardless of how many
snapshots reference it.

**ProjectData is the only per-snapshot cost.** At 2–5MB per snapshot,
20 snapshots adds ~100MB — negligible compared to the audio. Audio files
accumulate in `media/` but are never duplicated.

**Storage independence.** The root path (`~/BandTracker`) is configurable.
Moving this folder to Dropbox or iCloud Drive requires only updating
the root path in BandTracker's settings — no structural changes.

**Atomic writes for coordination files.** `handoff.json` is always written
by: write to `handoff.json.tmp`, then `rename()` to `handoff.json`.
`rename()` is atomic on POSIX systems. This prevents the other machine
reading a partial write during sync.

**Names are display, UUIDs are identity.** The project folder is named
after the song (human-readable). The `uuid` field in `project.json` is
the stable internal identity that survives renames. BandTracker never
exposes UUIDs to musicians.
