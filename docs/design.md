# System design

Status: implemented, single-owner private service. Updated 2026-09-29.

## Responsibilities

ChatGPT interprets photos and conversation, identifies foods and portions, and
presents uncertainty. The service validates structured inputs, stores audited
facts, resolves pinned inventory portions, and calculates nutrition and energy
results. Photos, image recognition, stock management, medical advice, multi-user
accounts, and a browser UI are outside this application's scope.

The current contracts are maintained in:

- [Nutrition log API](nutrition-api.md): entries, trainings, windows, goals,
  summaries, activity plans, and errors.
- [Food inventory API](food-inventory-api.md): reusable foods, provenance,
  portion resolution, and historical links.
- [Energy policy](../src/mcp_nutrition_db/energy-credit-policy.md): the standalone
  accounting specification, packaged verbatim for the policy tool.
- [Offline USDA reference](usda-reference.md): dataset coverage and updates.
- [Local testing](local-testing.md): development and tunnel checks.

The implementation plan tracks remaining work and historical verification;
it does not override these contracts.

## Runtime architecture

```text
ChatGPT -> OpenAI Secure MCP Tunnel <- tunnel-client on kage
                                          |
                                   loopback HTTP /mcp
                                          |
                                    FastMCP tools
                                          |
                         repository / inventory / USDA adapters
                                          |
                        personal SQLite + read-only USDA SQLite
```

The application and tunnel client are separate systemd services. The application
listens on loopback, and the tunnel initiates outbound connectivity. Application
packaging owns neither tunnel credentials nor the tunnel lifecycle. The sibling
`flakes` repository owns production integration and deployment pins.

## Code boundaries

| Module | Responsibility |
| --- | --- |
| `models`, `inventory_models` | Validated inputs, patch rules, calendar windows |
| `server`, `inventory_server` | Explicit MCP signatures, descriptions, registration |
| `tool_support`, `observability` | Shared execution/error boundary and redacted logs |
| `database`, `migrations` | Connection ownership, transactions, numbered schema changes |
| `repository` | Entry/training/goal persistence and summary orchestration |
| `entries` | Canonical entry snapshots and audit reads inside a caller-owned transaction |
| `inventory` | Catalog revisions, portion resolution, atomic history links |
| `energy` | Shared policy parameters, credited burn, policy metadata/text |
| `energy_calculation` | Pure chronological calculation from typed daily facts |
| `energy_response` | Public JSON serialization of calculated ledger state |
| `energy_repository` | Load one database snapshot into calculator inputs |
| `nutrients`, `serialization`, `errors` | Precision, aggregation, canonical encodings, domain errors |
| `usda_normalization`, `usda_dataset` | Independent bulk normalization and build-time import |
| `usda` | Offline search and immutable evidence copies |
| `backup`, `cli` | Atomic online snapshots and process lifecycle |

Domain writes remain explicit. Shared helpers handle repeated mechanics without
hiding SQL and mutation semantics behind a generic CRUD framework. Inventory and
entry operations share a caller-owned transaction when logging or linking food;
neither opens a competing write transaction for that work.

Energy calculation receives typed daily facts and effective-dated goals. It walks
chronologically from the earliest relevant goal through the requested range plus
three recovery days. Pending reservations and debit are explicit ledger state;
serialization happens after destination allocations have been resolved. No
mutable running balance is persisted. Corrections therefore recalculate from
current audited facts. See the policy for all arithmetic and settlement rules.

## Persistence and compatibility

The personal database uses SQLite with foreign keys, WAL, and a busy timeout.
Each request connection is explicitly closed on success and failure. The CLI owns
one idle connection for the server lifetime to keep WAL sidecars available to
the read-only backup sandbox; it holds no transaction and closes on shutdown. Mutations acquire
`BEGIN IMMEDIATE` and commit or roll back as a unit. File-backed databases are
required. The application does not support `:memory:` connections.

Numbered migration SQL is immutable. One runner obtains the write lock before
checking the applied versions and commits each schema change with its version
marker. Failures roll back that migration, permitting a clean retry. A newer
schema is rejected. Startup migration is repeatable and runs before MCP serves
requests. Schema v5 remains compatible with deployed data; the maintenance
refactor requires no record conversion or schema bump.

Energy and nutrient mass are stored as scaled integers with half-up rounding;
unknown values remain null. Aggregation preserves partial totals and propagates
component completeness. UUIDv4 identifiers are opaque.

Current records, immutable audit snapshots, and exact-create retry fingerprints
are separate. Catalog revisions and selected USDA evidence are copied snapshots,
so replacing a dataset or editing a catalog item cannot silently change a meal.
The inventory API specifies identity reservations and historical linking.

## Operations and verification

The flake exports the application, USDA dataset, development shell, NixOS module,
and package/module/quality checks. `nix flake check` runs package tests, formatting,
lint, type checks, and the NixOS module build. The module supports package,
listener, timezone, logging, state directory, USDA path, and backup overrides.
Its default listener is `127.0.0.1:8787`, readiness is `/healthz`, and state is
`/var/lib/mcp-nutrition-db/nutrition.sqlite3`.

The service uses `DynamicUser`, a mode-0700 state directory, and systemd hardening.
Loopback and the private tunnel form the single-owner access boundary. Production
credentials are sops-managed and passed to the tunnel through systemd credentials;
they never belong in this repository or the Nix store.

Application tool logs include tool name, outcome, duration, MCP request ID,
correlation ID, and original error class. Arguments, results, notes, and credentials
are excluded. The shared tool boundary translates expected errors consistently.

Backups use SQLite's online API, `PRAGMA quick_check`, and atomic replacement of a
mode-0600 snapshot. The optional weekly systemd timer maintains 26 weeks of local
rdiff history by default. Restore only while the application and tunnel are stopped,
after preserving existing state; restore to a temporary directory and verify
integrity before replacement. Detailed operational runbook work remains in the
implementation plan.

Tests cover validation, rollback/retry, concurrent migration and catalog creation,
audit history, calendar boundaries, completeness, policy conservation/corrections,
MCP schemas and protocol calls, and offline dataset replacement. Deployment checks
add a backup, compatibility verification on a copy, one activation, and live MCP
and service validation. Private snapshots and verification output stay outside Git.

## Decision log

| Date | Decision | Consequence |
| --- | --- | --- |
| 2026-08-27 | ChatGPT performs image interpretation; the service accepts structured estimates. | No image storage or vision dependency is needed. |
| 2026-08-27 | Use SQLite with scaled integer values. | Simple operations and exact aggregation; one active writer deployment is assumed. |
| 2026-08-27 | Use complete component-list replacement for entry corrections. | Updates are easy for an agent to reason about and audit. |
| 2026-08-27 | Use revisions, retry fingerprints, and immutable snapshots. | Retries and conversational corrections do not silently corrupt records. |
| 2026-08-27 | Use OpenAI Secure MCP Tunnel instead of nginx and a secret URL. | No public listener is needed; use is private/developer-mode only. |
| 2026-08-27 | Keep tunnel lifecycle in the deployment repo, separate from the application module. | The application flake remains reusable and does not own OpenAI credentials. |
| 2026-08-27 | Default calendar behavior to `Europe/Zurich`. | Results match the user's intended day rather than the server's Tokyo timezone. |
| 2026-08-27 | Make relative calendar windows first-class list and summary inputs. | ChatGPT can query `today` without calculating RFC 3339 boundaries. |
| 2026-08-27 | Replace required caller idempotency keys with short-lived server-side exact-replay detection. | Normal LLM calls are simpler while lost-response retries remain safe. |
| 2026-08-27 | Require nutrition provenance on every component. | Estimates, labels, restaurant declarations, and other sources remain distinguishable. |
| 2026-08-27 | Initial release derived calories from base burn plus training burn minus deficit. | This behavior is superseded by `energy-credit/v1`. |
| 2026-08-30 | Adopt the versioned `energy-credit/v1` policy as the replacement energy model. | Confidence-adjusted exercise is an optional allowance; sufficiently large unused credit can create capped, non-recurring allowances over the next three days. |
| 2026-08-31 | Replace v1 recovery scheduling with `energy-credit/v2`. | Cap the recoverable source pool before its 50%/30%/20% split, preserving a taper for isolated large days while retaining proportional destination collisions. |
| 2026-09-10 | Adopt `energy-credit/v3`: surplus above maintenance, bounded extra restriction with variable duration, and protected recovery first. | Schema v4 adds explicit, audited day reviews. Debit starts 2026-09-09 and applies without trip classification or return-date questions; repeated overshoots extend duration, not daily restriction. |
| 2026-09-10 | Adopt `energy-credit/v4`: settle past local days from current logged intake on query. | No timer or daily confirmation. Empty and unknown-calorie days cannot repay; backdated changes rebuild subsequent debit and recovery. Optional activity plans retain revision protection. |
| 2026-09-13 | Adopt `energy-credit/v5`: settle unused unadjusted budget, including incoming recovery, against opening debit. | Reserve source recovery before repayment to avoid double counting; distinguish incoming recovery repaid from expired. Preserve restriction pauses and caps. Serve the complete standalone policy in the MCP response. |
| 2026-09-24 | Adopt the [fixed food inventory API](food-inventory-api.md): inventory-first meal components, USDA-first composition, immutable food revisions, and deterministic short-name/USDA uniqueness. | Schema v5 is additive; historical conversion is individually reviewed and applied through MCP link plans after a verified backup. |
| 2026-09-24 | Serve USDA exclusively from a checksum-pinned local SQLite reference database built from official bulk releases. Copy selected source records and nutrients into immutable personal evidence/inventory revisions. | No live USDA API requests or keys. Dataset replacement affects discovery only; adopting changed composition requires an explicit inventory revision. Removed upstream records and old dataset garbage collection cannot erase copied evidence or change meals. See [offline USDA data](usda-reference.md). |

| 2026-09-29 | Separate database lifecycle, typed energy calculation, and shared MCP execution; keep one packaged policy text and dedicated API references. | Preserve schema v5 and energy arithmetic, fix completeness/null validation, and enforce quality checks in CI. |
