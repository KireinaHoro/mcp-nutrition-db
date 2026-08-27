# Implementation plan

- Status: Active
- Last updated: 2026-08-27
- Current phase: Phase 1 — application foundation

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

- [ ] Add `flake.nix` and `flake.lock` with package, app, dev shell, and check
  outputs for supported Linux systems.
- [ ] Create the Python package layout and a minimal executable entry point.
- [ ] Lock the official MCP SDK, Pydantic, and test dependencies.
- [ ] Add formatting, linting, type-checking, and pytest configuration.
- [ ] Add a minimal MCP server with a loopback-safe configurable bind address.
- [ ] Add repository contribution commands to the README.
- [ ] Add CI that runs the same checks exported by the flake.

Acceptance criteria:

- [ ] `nix flake check` passes from a clean checkout.
- [ ] `nix run . -- --help` succeeds.
- [ ] The development server can complete MCP initialization and list a
  placeholder or initial tool through Streamable HTTP.
- [ ] No runtime dependency is fetched outside the Nix build closure.

## Phase 2 — schema and repository

- [ ] Implement numbered, transactional SQLite migrations.
- [ ] Create entries, components, entry revisions, goals, goal revisions,
  idempotency keys, and schema migration tables.
- [ ] Enable foreign keys, WAL, busy timeout, and explicit transaction handling.
- [ ] Implement scaled-integer conversion and bounded decimal validation.
- [ ] Implement create/get/list/update/soft-delete repository operations.
- [ ] Implement effective-dated goal set/get operations.
- [ ] Implement idempotency-key digest and replay behavior.
- [ ] Implement optimistic revision conflicts and immutable audit snapshots.
- [ ] Add indexes and verify representative query plans.

Acceptance criteria:

- [ ] A new database migrates from zero to the current schema automatically.
- [ ] Reapplying migrations is safe.
- [ ] Repository tests cover success, rollback, retry, conflict, deletion, and
  constraint behavior.
- [ ] A failed multi-table write leaves no partial state.
- [ ] Decimal round trips and aggregate totals are deterministic.

## Phase 3 — MCP tool contracts

- [ ] Implement `nutrition_log_entry`.
- [ ] Implement `nutrition_get_entry`.
- [ ] Implement `nutrition_update_entry`.
- [ ] Implement `nutrition_delete_entry`.
- [ ] Implement `nutrition_list_entries` with bounded cursor pagination.
- [ ] Implement `nutrition_summarize` with timezone-aware grouping and
  completeness metadata.
- [ ] Implement `nutrition_set_goals`.
- [ ] Implement `nutrition_get_goals`.
- [ ] Add correct read-only, mutating, destructive, and idempotence annotations.
- [ ] Map domain failures to concise, structured agent-usable errors.

Acceptance criteria:

- [ ] Generated input schemas reject unknown fields and invalid units, dates,
  enum values, non-finite values, negatives, and excessive ranges.
- [ ] Every tool has contract tests for success and expected failures.
- [ ] Retrying a create with the same idempotency key cannot duplicate a meal.
- [ ] A stale update cannot overwrite a newer correction.
- [ ] Entry totals always equal server aggregation of current components.
- [ ] Summaries behave correctly across Europe/Zurich daylight-saving changes.

## Phase 4 — production runtime

- [ ] Add configuration for database path, bind address, port, default timezone,
  log level, and query limits.
- [ ] Refuse non-loopback binding by default and require an explicit unsafe
  override for any broader address.
- [ ] Add startup migration and database readiness checks.
- [ ] Add loopback health/readiness endpoints or SDK-supported equivalents.
- [ ] Add structured journald-friendly logging with correlation IDs and content
  redaction.
- [ ] Handle SIGTERM and drain in-flight requests within systemd stop timeout.
- [ ] Add HTTP-level and MCP-client integration tests.

Acceptance criteria:

- [ ] The server survives concurrent reads and serialized writes without
  database-lock leakage to the MCP client.
- [ ] Logs contain no raw request bodies, nutrition notes, tunnel credentials,
  or API keys.
- [ ] Graceful shutdown leaves the database consistent.
- [ ] An integration test exercises create, correct, query, summarize, goal, and
  delete operations over Streamable HTTP.

## Phase 5 — application NixOS module

- [ ] Export `nixosModules.default` from this flake.
- [ ] Define documented module options for package, listener, timezone, and
  state directory.
- [ ] Create a hardened systemd service with persistent state ownership.
- [ ] Ensure service startup waits for successful migrations.
- [ ] Add a NixOS evaluation or VM test for the module.
- [ ] Document local module use and state paths.

Acceptance criteria:

- [ ] A minimal NixOS configuration importing the module evaluates and builds.
- [ ] The unit binds only to `127.0.0.1:8787` under its defaults.
- [ ] The service can write only its intended state directory under systemd
  hardening.
- [ ] Restarting or upgrading preserves and migrates existing data.

## Phase 6 — `kage` and Secure MCP Tunnel deployment

Work in the sibling `flakes` repository and reference its commit in this
project's progress log.

- [ ] Add this repository as a locked flake input following the deployment
  repository's `nixpkgs`.
- [ ] Add and lock `github:nakasyou/openai-secure-tunnel-nix`.
- [ ] Import both NixOS modules in `nixos/kage/default.nix`.
- [ ] Enable `services.mcp-nutrition-db` on loopback.
- [ ] Configure the nutrition tunnel instance with the revealed tunnel ID.
- [ ] Pass `mcp-nutrition-db-tunnel-apikey` only through its sops secret file.
- [ ] Order the tunnel client after and require the nutrition service.
- [ ] Keep the nginx virtual host unchanged; add no MCP location.
- [ ] Include online SQLite snapshots in the host backup workflow.
- [ ] Build the complete `kage` system closure before deployment.
- [ ] Deploy and inspect both units, loopback listeners, and redacted logs.

Acceptance criteria:

- [ ] The Nix store and evaluated unit definitions contain neither the API key
  nor decrypted secret material.
- [ ] No new public port or nginx path exposes the MCP service.
- [ ] `mcp-nutrition-db` is healthy before `tunnel-client-nutrition` handles
  requests.
- [ ] A tunnel client restart reconnects without restarting or corrupting the
  application database.
- [ ] The pinned third-party tunnel package builds in the `kage` closure.

Local pre-deployment tunnel testing may use the gitignored `./api-key`. This is
an ephemeral development credential only; CI must not require it and production
must use the sops-managed key.

## Phase 7 — end-to-end ChatGPT validation

- [ ] Register/connect the OpenAI-hosted tunnel endpoint in ChatGPT developer
  mode for the intended account or workspace.
- [ ] Verify ChatGPT can discover all eight tools with correct descriptions and
  annotations.
- [ ] Log a meal from a photo and inspect the stored assumptions and confidence.
- [ ] Clarify a portion and verify the original entry is revised, not duplicated.
- [ ] Query recent entries and a date-range summary.
- [ ] Set goals and verify current and historical goal comparisons.
- [ ] Test destructive-tool confirmation and soft deletion.
- [ ] Verify a stale-revision conflict leads ChatGPT to refresh before retrying.
- [ ] Verify tunnel and service failures produce understandable user-facing
  behavior without exposing internal details.
- [ ] Run the local tunnel integration suite with `./api-key` when available,
  without exposing its contents in process arguments or logs.

Acceptance criteria:

- [ ] The full photo-to-log-to-correction lifecycle works through ChatGPT Web.
- [ ] Database inspection matches the conversational results and audit history.
- [ ] The service is still unreachable directly from the public internet.
- [ ] Limitations about visual estimates are visible in normal interaction.

## Phase 8 — operations and first release

- [ ] Add an online backup command or service/timer.
- [ ] Perform and document a restore rehearsal using a temporary directory.
- [ ] Write an operator runbook for status, logs, migrations, backup, restore,
  credential rotation, and tunnel reconnection.
- [ ] Document data export and permanent deletion procedures.
- [ ] Review dependency closures and licenses.
- [ ] Revoke the temporary development tunnel key and remove `./api-key` from
  local worktrees after tunnel-dependent development is complete.
- [ ] Tag the first release and pin it in the deployment repository.

Acceptance criteria:

- [ ] A backup has been restored and queried successfully.
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
