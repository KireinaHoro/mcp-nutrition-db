# Implementation plan

- Status: Active
- Last updated: 2026-08-30
- Current phase: Phase 2–4 and 3A — repository hardening and energy-credit accounting

This document is the project progress tracker. Update task checkboxes and the
progress log in the same commit as completed work. A phase is complete only when
all its acceptance criteria pass; partial work remains unchecked.

## Status legend

- `[ ]` not started or not yet verified
- `[x]` implemented and verified
- `BLOCKED:` cannot proceed without an external decision or state change

## Phase 0 — design baseline

- [x] Define the product boundary between ChatGPT and the service.
- [x] Define the MCP tool surface and mutation semantics.
- [x] Choose SQLite and document its schema strategy.
- [x] Choose Secure MCP Tunnel instead of a public nginx route.
- [x] Review the intended `kage`, sops, and flake integration points.
- [x] Record architecture, security, backup, and testing decisions.

Acceptance criteria:

- [x] `docs/design.md` is sufficient to implement without recovering decisions
  from chat history.
- [x] The implementation plan has testable phases and a progress log.

## Phase 1 — application foundation

- [x] Add `flake.nix` and `flake.lock` with package, app, dev shell, and check
  outputs for supported Linux systems.
- [x] Create the Python package layout and a minimal executable entry point.
- [x] Lock the official MCP SDK, Pydantic, and test dependencies.
- [x] Add formatting, linting, type-checking, and pytest configuration.
- [x] Add a minimal MCP server with a loopback-safe configurable bind address.
- [x] Add repository contribution commands to the README.
- [x] Add CI that runs the same checks exported by the flake.

Acceptance criteria:

- [x] `nix flake check` passes from a clean checkout.
- [x] `nix run . -- --help` succeeds.
- [x] The development server can complete MCP initialization and list a
  placeholder or initial tool through Streamable HTTP.
- [x] No runtime dependency is fetched outside the Nix build closure.

## Phase 2 — schema and repository

- [x] Implement numbered, transactional SQLite migrations.
- [x] Create entries, components, entry revisions, goals, goal revisions,
  create fingerprints, and schema migration tables.
- [x] Add training records, revisions, retry fingerprints, and migration from
  fixed calorie targets to base-burn energy goals.
- [x] Enable foreign keys, WAL, busy timeout, and explicit transaction handling.
- [x] Implement scaled-integer conversion and bounded decimal validation.
- [x] Implement create/get/list/update/soft-delete repository operations.
- [x] Implement effective-dated goal set/get operations.
- [x] Implement revision-safe training create/get/list/update/soft-delete
  operations and daily burn aggregation.
- [x] Implement canonical create-payload fingerprints, ten-minute exact-replay
  suppression, and explicit `force_new` behavior.
- [x] Implement optimistic revision conflicts and immutable audit snapshots.
- [ ] Add indexes and verify representative query plans.

Acceptance criteria:

- [x] A new database migrates from zero to the current schema automatically.
- [x] Reapplying migrations is safe.
- [ ] Repository tests cover success, rollback, retry, conflict, deletion, and
  constraint behavior.
- [ ] A failed multi-table write leaves no partial state.
- [ ] Decimal round trips and aggregate totals are deterministic.

## Phase 3 — MCP tool contracts

- [x] Implement `nutrition_log_entry`.
- [x] Implement `nutrition_get_entry`.
- [x] Implement `nutrition_update_entry`.
- [x] Implement `nutrition_delete_entry`.
- [x] Implement `nutrition_list_entries` with bounded cursor pagination.
- [x] Implement `nutrition_summarize` with timezone-aware grouping and
  completeness metadata.
- [x] Implement `nutrition_set_goals`.
- [x] Implement `nutrition_get_goals`.
- [x] Implement the five training tools and derived daily calorie budgets.
- [x] Add correct read-only, mutating, destructive, and idempotence annotations.
- [x] Map domain failures to concise, structured agent-usable errors.

Acceptance criteria:

- [ ] Generated input schemas reject unknown fields and invalid units, dates,
  enum values, non-finite values, negatives, and excessive ranges.
- [x] Component schemas require valid nutrition provenance and preserve source
  details through create, update, get, and revision snapshots.
- [ ] Every tool has contract tests for success and expected failures.
- [x] Retrying an identical create within ten minutes returns the original entry
  without requiring the caller to reproduce a token.
- [x] `force_new` can deliberately create an otherwise identical entry.
- [x] A stale update cannot overwrite a newer correction.
- [x] Entry totals always equal server aggregation of current components.
- [x] List and summary accept `today` and return their resolved interval without
  caller-side timestamp calculation.
- [x] Relative-day queries behave correctly across Europe/Zurich
  daylight-saving changes using an injected test clock.

## Phase 3A — exercise and recovery energy-credit accounting

- [x] Define and version the accepted policy in
  `docs/energy-credit-policy.md`.
- [x] Migrate training provenance to store reported burn, measurement method,
  confidence, and supporting evidence without losing schema-v2 history.
- [x] Add versioned policy constants, an explicit current-policy calculation
  basis, and deterministic calculations for credited burn, same-day use,
  recovery schedules, caps, and expiry.
- [x] Expose reported and credited burn separately in training and summary
  responses.
- [x] Add a read-only `nutrition_get_energy_policy` tool and return `policy_id`
  from every policy-derived result.
- [x] Update server instructions and affected tool descriptions so allowances
  are not presented as required intake targets.
- [x] Recalculate the source day and the following three local days after a
  relevant meal, training, goal, or policy correction.

Acceptance criteria:

- [x] High-, medium-, and low-confidence records receive exactly 100%, 80%, and
  60% credit, with no implicit source-specific multiplier.
- [x] Incoming recovery is consumed before same-day exercise allowance and
  cannot recursively create recovery credit.
- [x] Every positive amount of unused credited exercise is weighted
  50%/30%/20% over the next three days.
- [x] Each destination is independently capped at its planned deficit; clipped
  overflow and missed allocations expire without redistribution. Colliding
  allocations share the aggregate destination cap proportionally.
- [x] Corrections and deletion deterministically update all affected days while
  retaining auditable source facts.
- [x] Unit, repository, migration, MCP contract, and Streamable HTTP tests cover
  the examples and invariants in the policy document.
- [ ] ChatGPT reports the ordinary target, recovery allowance, and exercise
  allowance distinctly in an end-to-end production test.

## Phase 4 — production runtime

- [ ] Add configuration for database path, bind address, port, default timezone,
  log level, and query limits.
- [x] Refuse non-loopback binding by default and require an explicit unsafe
  override for any broader address.
- [x] Add startup migration and database readiness checks.
- [x] Add loopback health/readiness endpoints or SDK-supported equivalents.
- [x] Add structured journald-friendly logging with correlation IDs and content
  redaction.
- [ ] Handle SIGTERM and drain in-flight requests within systemd stop timeout.
- [ ] Add HTTP-level and MCP-client integration tests.

Acceptance criteria:

- [ ] The server survives concurrent reads and serialized writes without
  database-lock leakage to the MCP client.
- [x] Logs contain no raw request bodies, nutrition notes, tunnel credentials,
  or API keys.
- [ ] Graceful shutdown leaves the database consistent.
- [ ] An integration test exercises create, correct, query, summarize, goal, and
  delete operations over Streamable HTTP.

## Phase 5 — application NixOS module

- [x] Export `nixosModules.default` from this flake.
- [x] Define documented module options for package, listener, timezone, and
  state directory.
- [x] Create a hardened systemd service with persistent state ownership.
- [x] Ensure service startup waits for successful migrations.
- [x] Add a NixOS evaluation or VM test for the module.
- [x] Document local module use and state paths.

Acceptance criteria:

- [x] A minimal NixOS configuration importing the module evaluates and builds.
- [x] The unit binds only to `127.0.0.1:8787` under its defaults.
- [ ] The service can write only its intended state directory under systemd
  hardening.
- [ ] Restarting or upgrading preserves and migrates existing data.

## Phase 6 — `kage` and Secure MCP Tunnel deployment

Work in the sibling `flakes` repository and reference its commit in this
project's progress log.

- [x] Add this repository as a locked flake input following the deployment
  repository's `nixpkgs`.
- [x] Add and lock `github:nakasyou/openai-secure-tunnel-nix`.
- [x] Import both NixOS modules in `nixos/kage/default.nix`.
- [x] Enable `services.mcp-nutrition-db` on loopback.
- [x] Configure the nutrition tunnel instance with the revealed tunnel ID.
- [x] Pass `mcp-nutrition-db-tunnel-apikey` only through its sops secret file.
- [x] Order the tunnel client after and require the nutrition service.
- [x] Keep the nginx virtual host unchanged; add no MCP location.
- [x] Include online SQLite snapshots in the host backup workflow.
- [x] Build the complete `kage` system closure before deployment.
- [x] Deploy and inspect both units, loopback listeners, and redacted logs.

Acceptance criteria:

- [x] The Nix store and evaluated unit definitions contain neither the API key
  nor decrypted secret material.
- [x] No new public port or nginx path exposes the MCP service.
- [x] `mcp-nutrition-db` is healthy before `tunnel-client-nutrition` handles
  requests.
- [x] A tunnel client restart reconnects without restarting or corrupting the
  application database.
- [x] The pinned third-party tunnel package builds in the `kage` closure.

Local pre-deployment tunnel testing may use the gitignored `./api-key`. This is
an ephemeral development credential only; CI must not require it and production
must use the sops-managed key.

## Phase 7 — end-to-end ChatGPT validation

- [x] Register/connect the OpenAI-hosted tunnel endpoint in ChatGPT developer
  mode for the intended account or workspace.
- [x] Verify ChatGPT can discover all thirteen tools with correct descriptions
  and annotations after the training-capable build is deployed.
- [ ] Refresh ChatGPT discovery and verify the schema-v3 build exposes all
  fourteen tools, including `nutrition_get_energy_policy` and the revised
  training contract.
- [x] Log a meal from a photo and inspect the stored assumptions and confidence.
- [x] Clarify a portion and verify the original entry is revised, not duplicated.
- [x] Query recent entries and a date-range summary.
- [x] Query today's entries and summary using the relative-day window and verify
  the returned resolved interval.
- [x] Set goals and verify current and historical goal comparisons.
- [x] Set base burn and deficit, log and correct a training session, and verify
  the daily calorie budget changes by the corrected training burn.
- [x] Test destructive-tool confirmation and soft deletion.
- [x] Verify a stale-revision conflict leads ChatGPT to refresh before retrying.
- [ ] Verify tunnel and service failures produce understandable user-facing
  behavior without exposing internal details.
  - Known issue: with the local tunnel client stopped, ChatGPT remained on
    “Verbindung zur App wird hergestellt” for more than five minutes without an
    actionable timeout. Manual cancellation did not clearly prevent a queued
    read from being delivered after reconnection. This behavior is upstream of
    the loopback MCP service and must be rechecked against future ChatGPT/tunnel
    releases.
- [x] Run the local tunnel integration suite with `./api-key` when available,
  without exposing its contents in process arguments or logs.

Acceptance criteria:

- [x] The full photo-to-log-to-correction lifecycle works through ChatGPT Web.
- [x] Database inspection matches the conversational results and audit history.
- [x] The service is still unreachable directly from the public internet.
- [ ] Limitations about visual estimates are visible in normal interaction.

## Phase 8 — operations and first release

- [x] Add an online backup command and reusable incremental backup service/timer.
- [x] Perform and document a restore rehearsal using a temporary directory.
- [ ] Write an operator runbook for status, logs, migrations, backup, restore,
  credential rotation, and tunnel reconnection.
- [ ] Document data export and permanent deletion procedures.
- [ ] Review dependency closures and licenses.
- [ ] Revoke the temporary development tunnel key and remove `./api-key` from
  local worktrees after tunnel-dependent development is complete.
- [ ] Tag the first release and pin it in the deployment repository.

Acceptance criteria:

- [x] A backup has been restored and queried successfully.
- [ ] API-key rotation is tested without rebuilding secret material into Nix.
- [ ] The temporary development key is revoked and absent from release inputs.
- [ ] The runbook is sufficient to diagnose the service while preserving data.
- [ ] The deployed revision is a tagged, reproducible flake input.

## Deferred backlog

- [ ] Optional USDA or other nutrition database lookups with source attribution.
- [ ] Barcode import.
- [ ] Photo/object attachment storage with an explicit retention policy.
- [ ] Data export in a documented interoperable format.
- [ ] Multi-user identity, tenant isolation, and OAuth 2.1.
- [ ] Public plugin packaging and review, if a public use case ever exists.
- [ ] Dedicated metrics and alerting.

Items move out of this section only after a design update defines their privacy,
security, data-model, and deployment consequences.

## Definition of done

The first release is done when Phases 1 through 8 meet their acceptance
criteria, all checks pass from a clean checkout, the exact pinned package is
deployed on `kage`, the end-to-end ChatGPT workflow is verified, and backup
restore has been rehearsed. Passing local unit tests alone is not completion.

## Progress log

| Date | Phase | Change | Evidence / reference |
| --- | --- | --- | --- |
| 2026-08-27 | 0 | Materialized the accepted design and implementation plan. | `README.md`, `docs/design.md`, `docs/implementation-plan.md` |
| 2026-08-27 | 0 | Incorporated review feedback on relative-day queries, server-side retry deduplication, and per-component nutrition provenance. | Design decision log and Phases 2, 3, and 7 |
| 2026-08-27 | 1–4 | Implemented the first local MVP: pinned Python environment, SQLite schema/repository, all eight MCP tools, readiness, and 14 passing tests including an MCP process session. | `src/`, `tests/`, `flake.nix`; Streamable HTTP and Nix closure checks remain pending |
| 2026-08-27 | 1, 4–5 | Verified all eight tools through Streamable HTTP, passed the full flake check and NixOS closure build, and documented direct Codex attachment. | `nix flake check --print-build-logs`; local MCP lifecycle test; `.codex/config.toml` |
| 2026-08-27 | 6–7 | Pinned the reviewed tunnel-client flake, passed `doctor`, connected the live local tunnel, and documented file-reference-only use of the temporary development key; a ChatGPT tool call remains pending. | `flake.lock`, `docs/local-testing.md`; tunnel-client startup metadata |
| 2026-08-27 | 7 | Completed ChatGPT connector discovery and the first photo-to-log call through the live tunnel. Read-only inspection confirmed one entry, four estimated components, medium confidence, explicit assumptions, and totals matching the conversation. | Local `nutrition.sqlite3`; redacted MCP and tunnel access logs |
| 2026-08-27 | 4, 7 | Confirmed ChatGPT revised the original meal to revision 2 and set today's goal. Fixed one-day whole-range summaries omitting the effective goal, added goal-progress regression coverage, and added redacted structured tool outcome logs. | Local database audit rows; repository and observability tests |
| 2026-08-27 | 7 | Verified a second photo meal and in-conversation correction: the breakfast advanced to revision 2, its original five-component snapshot remained intact, and current provenance spans estimates, user clarification, and a visible nutrition label. Today's two-entry summary correctly reports its resolved interval and goal progress. | Local database audit rows; live MCP summary; redacted tool logs |
| 2026-08-27 | 7 | Completed ChatGPT tests for relative-day and date-range queries, current and historical goals, stale-revision recovery, destructive confirmation, and soft deletion. Stopped the tunnel client for the remaining user-facing failure and recovery test while leaving the loopback backend healthy. | User-confirmed ChatGPT results; local tunnel test session |
| 2026-08-27 | 7 | Tested tunnel outage and recovery. ChatGPT showed a non-terminating connection message for over five minutes, so understandable failure behavior remains unmet. Restarting the tunnel restored normal calls without restarting the backend; redacted logs confirmed subsequent list/get operations succeeded. | User-observed ChatGPT behavior; tunnel and MCP structured logs |
| 2026-08-27 | 8 | Added atomic SQLite online snapshots and a backup-enabled NixOS module with weekly, persistent, 26-week rdiff history. A disposable two-generation repository restored two rows and passed `PRAGMA quick_check`. | Backup unit tests; NixOS closure; local rdiff restore rehearsal |
| 2026-08-27 | 2–4 | Added correctable training sessions and changed calorie accounting to base burn plus daily training burn minus deficit. Migrated a copy of the existing local database and exercised the flow through a real MCP client session. | Schema v2; 22 tests; full flake check; local-copy migration |
| 2026-08-27 | 6–8 | Deployed the schema-v2 service and Secure MCP Tunnel client on `kage`, sharing the deployment repository's nixpkgs. Verified loopback-only listeners, sops-backed systemd credentials, healthy redacted logs, the persistent weekly timer, two successful incremental backup runs, repository verification, and a restored SQLite `quick_check`. The first live rehearsal also fixed missing `procps` runtime wiring and nonfatal empty-retention handling. | App commits `3541b43`, `d377874`; flakes commits `9cf2827`, `05c8836`, `cd51374`; production systemd, health, rdiff verify/restore evidence |
| 2026-08-27 | 6 | Diagnosed production tunnel `401` responses with direct copied-credential tests. The client resolves only one secret-reference layer, so the module's `env:` to `file:` nesting sent the file reference as the bearer token. Kept the sops value in `LoadCredential` and pointed generated YAML directly at the runtime systemd credential; production then initialized MCP and fetched tunnel metadata successfully. | Flakes commit `a837de8`; direct raw-value and direct-file A/B tests; production control-plane logs |
| 2026-08-28 | 5–6 | Removed the tunnel client from this application's inputs and development shell. Local tunnel tests now consume the client exposed by the sibling deployment flake, which already owns the production package and module pin. | Application and deployment flake checks |
| 2026-08-30 | 5–7 | A direct Codex POST to the tunnel control-plane URL returned `404`, establishing that it is not a Streamable HTTP MCP endpoint. Restored project-scoped Codex to the local development listener; production remains available through the configured ChatGPT Tunnel surface. | Observed MCP initialization response; official Secure MCP Tunnel architecture documentation |
| 2026-08-30 | 0, 3A | Accepted and documented `energy-credit/v1`: confidence-adjusted exercise allowance, bounded non-recurring recovery credits, explicit MCP presentation, and an implementation checklist. | `docs/energy-credit-policy.md`; design decision log |
| 2026-08-30 | 2–4, 3A | Implemented schema v3 training provenance and the derived `energy-credit/v1` ledger, added the policy tool and MCP guidance, migrated a production snapshot successfully, and covered confidence, recovery, collision, correction, incomplete-intake, stdio, and Streamable HTTP behavior. | 29 tests; full flake check; migrated-copy `PRAGMA quick_check` |
| 2026-08-30 | 3A, 6–8 | Deployed schema v3 and `energy-credit/v1` to `kage`; verified both units, schema health, MCP initialization, all fourteen tools, the policy response, and today's production ledger. Audited the four migrated training records into explicit device, HR/GPS, or power-meter evidence and created a fresh incremental backup. | App commit `a5595f4`; flakes commit `a13bda0`; production MCP and systemd evidence |
