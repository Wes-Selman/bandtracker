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
| 2 — Bridge | 13 | Identity foundation + onboarding | ⬜ Not started |
| 2 — Bridge | 14 | Background daemon | ⬜ Not started |
| 3 — MVP front-end | 15 | Multi-person collaboration validation | ⬜ Not started |
| 3 — MVP front-end | 16 | Menu bar + full window SwiftUI app | ⬜ Not started |
| 4 — Post-launch | 17+ | Conflict resolution, cloud storage backend, full auth, push notifications, iPad | ⬜ Deferred |

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

## Increment 13 — Identity foundation + onboarding ⬜

**Goal:** Establish a persistent per-person identity on each device, and provide
a guided onboarding flow for both first-time solo users and incoming collaborators.

The right framing here is **people, not machines**. A person has a stable identity
that travels with them across devices. The local config file is v1 of what will
eventually be a proper auth token — the architecture must support that migration
without touching business logic.

**Delivers:** `bandtracker configure`, `bandtracker join`

`bandtracker configure` — first-time setup. Sets display name, identifier, and root
path. Written to `~/.bandtracker/config.json`. All commands read identity from this
file instead of requiring `--author` on every invocation. The identifier is an opaque
string — no format assumptions, no parsing. v1 is user-supplied (e.g. an email they
choose), designed to be replaced by a token in Phase 4 without changing anything else.

`bandtracker join <shared-folder-path>` — incoming collaborator onboarding. Points
BandTracker at an existing shared root, registers the person against the project using
their configured identity, and sets up the watcher. Does not re-initialize. Reads
the existing snapshot history as-is.

**Config file:** `~/.bandtracker/config.json`
```json
{
  "display_name": "Jordan",
  "identifier": "jordan@email.com",
  "root_path": "/Users/jordan/Dropbox/BandTracker"
}
```

**Design constraints:**
- `~/.bandtracker/config.json` is the only place that knows how identity was established
- All other code reads `config.identifier` opaquely — same as today with `BANDTRACKER_AUTHOR`
- `--author`, `--root`, and `BANDTRACKER_*` env vars remain as overrides for power users
- Auth is a drop-in replacement in Phase 4, not a refactor

**Onboarding scenarios covered:**
- Solo first-time user: `configure` then `init`
- Incoming collaborator: `configure` then `join <shared-folder>`
- Existing CLI user migrating: `configure` populates config, all existing projects continue working

---

## Increment 14 — Background daemon ⬜

**Goal:** `bandtracker watch` becomes a background process rather than a
foreground command. Core watcher logic is already decoupled from the CLI
invocation pattern in anticipation of this.

IPC between daemon and SwiftUI app via local socket or file-based events.

---

## Increment 15 — Multi-person collaboration validation ⬜

**Goal:** Verify the full collaboration flow works end to end between two real
people sharing a project folder via Dropbox or iCloud Drive. The primitive is
**people, not machines** — the test should cover both same-person-two-devices
and two-different-people scenarios, as these have different failure modes.

**Scenario A — Two people, shared folder:**
- Maya configures BandTracker, inits a project in a shared Dropbox folder
- Jordan configures BandTracker on his machine, joins via the shared folder path
- Maya saves in GarageBand → Jordan's watcher detects the change via FSEvents
- Maya hands off to Jordan — `handoff.json` written atomically, Jordan's client detects it
- Jordan makes changes, snapshots, hands back
- Maya reconciles on startup after Jordan's offline edits

**Scenario B — Same person, two devices:**
- Maya has BandTracker configured on her MacBook and her iMac
- Both point at the same shared root
- Verify no identity conflicts, no duplicate collaborator entries, correct lock behavior

**What may need to be built:**
- `StorageProvider` currently only has a working `local` implementation — `detect()`
  identifies iCloud and Dropbox paths but no provider-specific handling exists yet
- Sync delay handling — shared folders have propagation latency the watcher may need to tolerate

**Definition of done:** Two people, one shared folder, one GarageBand project, full
handoff cycle completed in both directions without manual intervention or data loss.

---

## Increment 16 — Menu bar + full window SwiftUI app ⬜

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
   It's a stable string that represents a person across all their devices. Established
   once at configure time, never parsed or validated by business logic. Designed so
   a future auth token can replace it without touching anything else.

3. **`bandtracker watch` decoupled from CLI** — nothing in `core/watcher.py` assumes
   it's being called from a terminal. Required for the Increment 14 daemon transition.

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
