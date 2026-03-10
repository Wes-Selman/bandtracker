# BandTracker — Incremental Delivery Plan

Each increment is independently useful and testable.
Pick up from any increment after a break — the status column tells you where things stand.

| Phase | # | Name | Status |
|-------|---|------|--------|
| 1 — CLI foundation | 0 | Repo structure + models | ✅ Complete |
| 1 — CLI foundation | 1 | Project initialization | ✅ Complete |
| 1 — CLI foundation | 2 | Snapshot writer | ✅ Complete |
| 1 — CLI foundation | 3 | Diff engine | ✅ Complete |
| 1 — CLI foundation | 4 | FSEvents watcher | ✅ Complete |
| 1 — CLI foundation | 5 | Reconciliation | ✅ Complete |
| 1 — CLI foundation | 6 | Restore | ✅ Complete |
| 1 — CLI foundation | 7 | Handoff | ✅ Complete |
| 1 — CLI foundation | 8 | Sidecar documents | ✅ Complete |
| 1 — CLI foundation | 9 | Project management | 🔄 In progress |
| 1 — CLI foundation | 10 | Diff command + structural fallback | ⬜ Not started |
| 2 — Bridge | 11 | band-cartographer evaluation | ⬜ Not started |
| 2 — Bridge | 12 | JSON output layer | ⬜ Not started |
| 2 — Bridge | 13 | Background daemon | ⬜ Not started |
| 3 — MVP front-end | 14 | Two-machine collaboration validation | ⬜ Not started |
| 3 — MVP front-end | 15 | Menu bar + full window SwiftUI app | ⬜ Not started |
| 4 — Post-launch | 16+ | Conflict resolution, shared storage, auth, push notifications, iPad | ⬜ Deferred |

---

## Increment 0 — Repo structure + models ✅

**Deliverables:**
- Full folder structure established
- `core/models.py` — all dataclasses with JSON serialization
- `core/diff/` — stub files with documented responsibilities
- `cli/main.py` — command router
- `tests/test_models.py` — full model test coverage
- `docs/folder-structure.md` — canonical folder reference

---

## Increment 1 — Project initialization ✅

**Delivers:** `bandtracker init`

Validates a `.band` bundle, copies it into `live/`, hashes media into the
content-addressed store, writes `project.json` and `handoff.json`, takes snapshot 001.

**Key files:** `core/init.py`, `cli/commands/init.py`, `tests/test_init.py`

---

## Increment 2 — Snapshot writer ✅

**Delivers:** `bandtracker snapshot`

Media deduplication, manifest.json, meta.json, atomic writes, milestone tags.

**Key files:** `core/snapshot.py`, `cli/commands/snapshot.py`, `tests/test_snapshot.py`

---

## Increment 3 — Diff engine ✅

**Delivers:** Auto-generated snapshot descriptions

Binary diff engine ported from band-cartographer. Decodes tempo, pan, structural
size-delta heuristic. Powers auto-descriptions in `bandtracker snapshot`,
`bandtracker watch`, and `bandtracker reconcile`.

Also delivers: `bandtracker learn-noise` — interactive noise mask builder.

**Key files:** `core/diff/engine.py`, `core/diff/noise.py`, `core/diff/interpreter.py`,
`cli/commands/learn_noise.py`, `tests/test_diff.py`

---

## Increment 4 — FSEvents watcher ✅

**Delivers:** `bandtracker watch`

Watchdog FSEvents watcher, debounced save detection, prompts "Save a version? [y/n]".
Designed to stay decoupled from the CLI invocation — will become a background daemon
in Increment 13.

**Key files:** `core/watcher.py`, `cli/commands/watch.py`, `tests/test_watcher.py`

---

## Increment 5 — Reconciliation ✅

**Delivers:** `bandtracker reconcile`

Detects offline edits at watcher startup. `core/bundle_ref.py` stores the GB bundle
path and macOS NSURL bookmark. `bandtracker set-gb` migration command for pre-Increment-5
projects.

**Key files:** `core/reconcile.py`, `core/bundle_ref.py`, `cli/commands/reconcile.py`,
`cli/commands/set_gb.py`, `tests/test_reconcile.py`

---

## Increment 6 — Restore ✅

**Delivers:** `bandtracker restore <n>`

Atomic rollback of `live/` and the original GB bundle. Takes a confirmation snapshot
after restore. GarageBand must be closed.

**Key files:** `core/restore.py`, `cli/commands/restore.py`, `tests/test_restore.py`

---

## Increment 7 — Handoff ✅

**Delivers:** `bandtracker handoff`, `bandtracker claim`, `bandtracker release`

Full lock state machine (OPEN/LOCKED). Silent FSEvents watcher on `handoff.json`
in ProjectWatcher surfaces handoff events without polling.

**Key files:** `core/handoff_ops.py`, `cli/commands/handoff.py`, `cli/commands/claim.py`,
`cli/commands/release.py`, `tests/test_handoff.py`

---

## Increment 8 — Sidecar documents ✅

**Delivers:** `bandtracker attach`, `bandtracker detach`, `bandtracker attachments`

Two attachment types: `version` (snapshot-pinned) and `project` (inherits forward
across snapshots, most recent copy wins via shadowing). Backward-compatible model
change — old plain-string sidecar entries read back as `type=version`.

**Key files:** `core/sidecar.py`, `cli/commands/attach.py`, `cli/commands/detach.py`,
`cli/commands/attachments.py`, `tests/test_sidecar.py`

---

## Increment 9 — Project management 🔄

**Delivers:** `bandtracker status`, `bandtracker log`, `bandtracker add-collaborator`,
`bandtracker remove-collaborator`, `bandtracker rename`

- `status` — current snapshot, lock state, who has the ball, unsaved changes indicator
- `log` — list snapshots with index, description, author, timestamp, milestone
- `add-collaborator --name "Maya" --id maya@email.com` — adds to `project.json`
- `remove-collaborator --id maya@email.com` — removes from `project.json`
- `rename <new-name>` — renames project folder and updates `project.json`

---

## Increment 10 — Diff command + structural fallback ⬜

**Delivers:** `bandtracker diff <n>`, `bandtracker diff <n> <m>`

Public-facing diff between any two snapshots. Also implements the structural
fallback tier in `build_description()` — replacing the current "Work in progress"
placeholder with a meaningful summary when interpretation fails:

```
"47 changes detected across 3 regions (+120 bytes)"
```

Three-tier description quality: interpreted → structural → identical.
Applies everywhere descriptions are generated — snapshot, reconcile, watch, diff.

---

## Increment 11 — band-cartographer evaluation ⬜

**Goal:** Assess the relationship between BandTracker and band-cartographer now
that the diff engine has a public-facing surface.

**Questions to answer:**
- Is interpreter coverage broad enough to warrant extracting as an installable package?
- What Logic Pro offsets can be mapped opportunistically?
- Should band-cartographer be restructured as a library (`bandcartographer` on PyPI)
  that BandTracker depends on, rather than a manual port?

**Decision point:** If coverage warrants it, restructure band-cartographer as a
package and replace `core/diff/interpreter.py` with a proper dependency.
If not, document what additional research is needed and defer.

---

## Increment 12 — JSON output layer ⬜

**Goal:** Every command that returns data gets a `--json` flag outputting clean
structured JSON. This is the contract the Swift app will consume.

Every subcommand gets `--json`. Output is stable, versioned, and documented.
Swift calls the CLI via `Process` and parses stdout.

---

## Increment 13 — Background daemon ⬜

**Goal:** `bandtracker watch` becomes a background process rather than a
foreground command. Core watcher logic is already decoupled from the CLI
invocation pattern in anticipation of this.

IPC between daemon and SwiftUI app via local socket or file-based events.

---

## Increment 14 — Two-machine collaboration validation ⬜

**Goal:** Verify the full collaboration flow works end to end across two real machines
sharing a project folder via Dropbox or iCloud Drive.

**What needs to be tested:**
- Both machines pointing `StorageProvider` at the same shared root
- Machine A saves in GarageBand → Machine B's watcher detects the change via FSEvents on the shared folder
- Handoff from Machine A to Machine B — `handoff.json` written atomically, Machine B detects the change
- Reconciliation on Machine B startup after Machine A made offline edits
- Snapshot taken on Machine A visible on Machine B after sync

**What may need to be built:**
- `StorageProvider` currently only has a working `local` implementation — `detect()` identifies
  iCloud and Dropbox paths but no provider-specific handling exists yet
- Sync delay handling — shared folders have propagation latency that the watcher may need to tolerate
- Cross-machine identifier agreement — both machines must use the same identifier string for the same person

**Definition of done:** Two machines, one shared folder, one GarageBand project, full
handoff cycle completed without manual intervention or data loss.

---

## Increment 15 — Menu bar + full window SwiftUI app ⬜

**Goal:** First non-technical user can use BandTracker without touching a terminal.

- Menu bar: always-visible status, handoff alerts (GarageBand is fullscreen most
  of the time — menu bar presence is essential)
- Full window: timeline, snapshot browser, collaborator management, sidecar files

Swift calls the CLI via `Process`, consumes `--json` output, watches for filesystem
changes via FSEvents.

Lives in `app/` within this repo.

---

## Architectural constraints

These apply to every increment:

1. **`--json` flag convention** — every command that returns data gets `--json`
   outputting clean structured JSON. Design output to be serializable from the start.

2. **`identifier` is always opaque** — never parse it as an email or specific format.
   It's a stable string both machines agree on. Protects a future auth migration.

3. **`bandtracker watch` decoupled from CLI** — nothing in `core/watcher.py` assumes
   it's being called from a terminal. Required for the Increment 13 daemon transition.

4. **Swift integration model** — Swift calls the CLI via `Process`, consumes `--json`
   output, watches filesystem via FSEvents. The CLI is the API.

5. **Storage abstraction is intentional** — `StorageProvider` exists for a reason.
   Never hardcode filesystem assumptions outside of `ProjectPaths`.

6. **Diff engine resilience — three-tier descriptions** — interpreted → structural →
   identical. The structural tier is always producible. Protects against GarageBand
   format updates and enables Logic Pro support from day one.

7. **Result dataclasses everywhere** — all core functions return a typed result with
   `.ok`, `.errors`, `.warnings`. No exceptions bubble to the CLI. No `sys.exit` in
   `core/`.
