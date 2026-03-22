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
| 1 — CLI foundation | 9 | Project management | ✅ Complete |
| 1 — CLI foundation | 10 | Diff command + structural fallback | ✅ Complete |
| 2 — Bridge | 11 | band-cartographer evaluation | ✅ Complete |
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
in Increment 14.

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

## Increment 9 — Project management ✅

**Delivers:** `bandtracker status`, `bandtracker log`, `bandtracker add-collaborator`,
`bandtracker remove-collaborator`, `bandtracker rename`

- `status` — current snapshot, lock state, who has the ball, unsaved changes indicator
- `log` — list snapshots with index, description, author, timestamp, milestone
- `add-collaborator --name "Maya" --id maya@email.com` — adds to `project.json`
- `remove-collaborator --id maya@email.com` — removes from `project.json`
- `rename <new-name>` — renames project folder, live bundle, GB bundle (best-effort),
  and updates `project.json` including `gb_bundle_path`/`gb_bundle_alias`

Also delivers: `cli/resolver.py` — shared CLI resolution for `--root`, `--project`,
`--author` flags and env vars. All existing commands updated to use it.

**Key files:** `core/project_ops.py`, `cli/resolver.py`, `cli/commands/status.py`,
`cli/commands/log.py`, `cli/commands/add_collaborator.py`,
`cli/commands/remove_collaborator.py`, `cli/commands/rename.py`,
`tests/test_project_ops.py`

---

## Increment 10 — Diff command + structural fallback ✅

**Delivers:** `bandtracker diff <n>`, `bandtracker diff <n> <m>`

Public-facing diff between any two snapshots, or between a snapshot and the
current GarageBand bundle. `diff <n>` resolves the GB bundle from project.json
(same as reconcile/watch) for accurate results regardless of watcher state.

Also implements the structural fallback tier in `build_description()` — three-tier
description quality: interpreted → structural → identical.

- Interpreted: `"tempo changed to 128 BPM; track added"`
- Structural: `"3 byte ranges changed, 48 bytes modified, +120 bytes net"`
- Identical: `"no changes detected"`

New `core/diff_ops.py` consolidates the diff pipeline (byte_diff → interpret_changes →
build_description) into a single `compare()` function returning a typed `CompareResult`.
Future increments can point snapshot/reconcile/watcher at this instead of their
inline pipelines.

**Key files:** `core/diff_ops.py`, `core/diff/engine.py`, `cli/commands/diff.py`,
`tests/test_diff_ops.py`

**Depends on:** Increment 3 (diff engine), Increment 9 (resolver pattern)

---

## Increment 11 — band-cartographer evaluation ✅

**Decision: Keep manual port.** band-cartographer stays a separate research repo.
Findings continue to be ported into `core/diff/interpreter.py` manually.

**Evaluation summary:**

*Coverage assessment:* The interpreter currently decodes tempo (4 offsets, fully
decoded with BPM value) and pan (1 offset, raw value only). FINDINGS.md in
band-cartographer has also confirmed mute (`0x1accf`), volume (`0xc5` + `0x1ace9`),
and time signature (`0xfa`, `0x3b6`) — these have not been ported into the interpreter
yet. Structural changes (add/remove track/region) are detected by a size-delta
heuristic, not by offset-based decoding.

*Package extraction rejected:* Only one GB version's data exists (10.4.8 arm64),
only one consumer (BandTracker), and the field map is small (~5 fields). Extracting
band-cartographer as a PyPI package would add CI/CD, versioning, and dependency
management overhead without proportional benefit. The interpreter is ~120 lines of
Python — manual porting takes minutes.

*Contributor accessibility:* band-cartographer is ~80% contributor-ready. The
experiment protocol, CLI tooling, CONTRIBUTING.md, and PR template are solid. Gaps
identified: no "good first experiment" signposting, no machine-readable field
registry (`fields.json`), FINDINGS.md tempo encoding is inconsistent with
BandTracker's confirmed BPM × 10,000 formula, and open questions aren't filed
as GitHub Issues. These improvements are deferred until BandTracker is socialized
and community contributions become realistic.

*Version branching:* Research output is already version-keyed
(`research/<version>_<arch>/`), but the consumption side has no version awareness.
When a second GB version's data exists, the interpreter will need a `gb_version`
parameter and a version-keyed field registry. `CbVersion` from the outer plist
(stored in `project.json` at init time) is the likely discriminator. The three-tier
fallback already handles unknown versions gracefully (drops to structural
descriptions). Deferred until cross-version data exists.

*Logic Pro:* The diff engine is format-agnostic and works on any binary blob.
Only the interpreter is GB-specific. Logic Pro `.logicx` bundles share format
ancestry but offsets would need independent mapping experiments. Deferred.

**Re-open triggers for package extraction:**
- A second GB version's research data exists, requiring version-branched offsets
- A second consumer of the field map appears (e.g., standalone Logic Pro tracker)
- Field map grows past ~20 fields, making inline maintenance unwieldy
- Community contributions to band-cartographer become active

**Porting backlog** (confirmed in FINDINGS.md, not yet in interpreter.py):
- Mute state: `0x1accf` (uint8, 1 byte)
- Volume: `0xc5` + `0x1ace9` (uint8/uint16, 2 ranges)
- Time signature: `0xfa`, `0x3b6` (uint32 LE, 2 locations)

**Future work logged (all deferred):**
- Version-keyed `fields.json` registry in band-cartographer
- Cross-version comparison tooling (`compare-versions` command)
- `CbVersion`-based version branching in BandTracker's interpreter
- Contributor on-ramp improvements (good-first-experiment labels, GitHub Issues)
- FINDINGS.md tempo encoding correction (should say BPM × 10,000, not µs/beat)

**No code changes. No test count change. 533 passing, 4 skipped.**

---

## Increment 12 — JSON output layer ⬜

**Goal:** Every command that returns data gets a `--json` flag outputting clean
structured JSON. This is the contract the Swift app will consume.

Every subcommand gets `--json`. Output is stable, versioned, and documented.
Swift calls the CLI via `Process` and parses stdout.

**Core API:** No new core functions — this is a CLI-layer change. Each command's
`run()` function checks `args.json`, and if set, serializes the result dataclass
via `dataclasses.asdict()` → `json.dumps()` to stdout instead of the human-readable
output. Result dataclasses are already JSON-safe by design (constraint #8).

**Files to read:**
- `cli/commands/status.py` — representative command with a result dataclass
- `cli/commands/log.py` — list output that needs JSON array form
- `cli/commands/diff.py` — CompareResult serialization
- `core/project_ops.py` — StatusResult, LogResult etc. for serialization shape
- `core/diff_ops.py` — CompareResult for serialization shape
- Every `cli/commands/*.py` file — all need `--json` added

**Depends on:** All result dataclasses being JSON-safe (enforced since Increment 0)

---

## Increment 13 — Identity foundation + onboarding ⬜

**Goal:** First-run config and `~/.bandtracker/config.json`. CLI resolution
order gains a config file step: flags → env vars → config file → error.

**Core API:** New `core/config.py` with `load_config()` / `save_config()`.
`cli/resolver.py` gains a config file fallback between env vars and error.

**Files to read:**
- `cli/resolver.py` — current resolution order (will gain config file step)
- `cli/commands/init.py` — onboarding flow, may need config prompts
- `core/models.py` — identifier model, to verify no format assumptions

**Depends on:** Increment 9 (resolver consolidation)

---

## Increment 14 — Background daemon ⬜

**Goal:** Convert `bandtracker watch` from foreground terminal process to a
launchd-managed background daemon. Core watcher logic is already decoupled from the CLI
invocation pattern in anticipation of this.

IPC between daemon and SwiftUI app via local socket or file-based events.

**Files to read:**
- `core/watcher.py` — ProjectWatcher, already CLI-decoupled
- `cli/commands/watch.py` — current foreground entry point
- `core/reconcile.py` — called at watcher startup, must work in daemon context

**Depends on:** Increment 4 (watcher), Increment 5 (reconcile at startup)

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
- Name reconciliation — if Machine A renames a project while Machine B is offline,
  Machine B should detect the mismatch between project.json name and folder name on
  next startup (see Bug C)
- Shared-storage policy for GB bundles — may require the GB bundle to live inside
  the BandTracker project folder for shared-storage projects (see Bug C)

**Files to read:**
- `core/models.py` — StorageProvider, StorageProviderType
- `core/watcher.py` — ProjectWatcher, _HandoffHandler
- `core/reconcile.py` — offline edit detection
- `core/handoff_ops.py` — lock state machine
- `core/project_ops.py` — rename_project() and its best-effort GB rename (Bug C)

**Depends on:** Increment 13 (identity/config), Increment 14 (daemon)

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

**Files to read:**
- `cli/main.py` — command list (each becomes a Swift-callable operation)
- Increment 12 output — JSON schemas for every command

**Depends on:** Increment 12 (JSON output), Increment 14 (daemon)

---

## Post-launch (Increment 17+) ⬜

**Deferred items:**
- Conflict resolution, cloud storage backend, full auth, push notifications, iPad
- **Branch/fork resolution** — When two machines diverge (e.g. both edit while
  offline), offer three resolution paths: (1) pick one timeline as canonical,
  (2) fork the divergent version into a new project ("view as new project"),
  (3) create a snapshot in one project from a specific snapshot in another.
  Depends on Increment 15 findings about real-world divergence patterns.

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

4. **Storage abstraction intentional** — `StorageProvider` and `ProjectPaths` exist
   for a reason. Never hardcode path assumptions outside these classes.

5. **Diff engine resilience — three-tier descriptions** — interpreted → structural →
   identical. The structural tier is always producible. Protects against GarageBand
   format updates and enables Logic Pro support from day one.

6. **Result dataclasses everywhere** — all core functions return a typed result with
   `.ok`, `.errors`, `.warnings`. No exceptions bubble to the CLI. No `sys.exit` in
   `core/`.