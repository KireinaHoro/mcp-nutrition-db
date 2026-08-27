# System design

- Status: Accepted for implementation
- Last updated: 2026-08-27
- Scope: single-owner private service

## 1. Purpose

The service gives ChatGPT durable tools for maintaining a personal nutrition
log. The primary interaction is conversational:

1. The user posts a meal photo or describes food in ChatGPT.
2. ChatGPT estimates portions and nutrition, stating material assumptions.
3. ChatGPT calls this MCP service with structured data.
4. The user can later clarify a portion, ingredient, or serving size, and
   ChatGPT updates the existing entry rather than creating a duplicate.
5. ChatGPT can query entries, summaries, and goals to answer questions about
   past intake.

This is a record-keeping system, not a medical device. Nutrition values derived
from images are estimates and must remain identifiable as such.

## 2. Product boundary

### The service is responsible for

- validating and storing structured meal, snack, and macro data;
- deriving entry totals from component values;
- querying individual entries and bounded date ranges;
- calculating daily and range summaries;
- storing effective-dated nutrition goals;
- supporting corrections with optimistic concurrency and an audit history;
- exposing those operations as MCP tools over Streamable HTTP.

### ChatGPT is responsible for

- understanding images and conversation context;
- estimating food identity, quantity, and nutrition;
- deciding which service tool to call;
- asking the user for clarification when uncertainty is material;
- presenting results and uncertainty in natural language.

### Out of scope for the first release

- storing meal photos or other binary attachments;
- doing image recognition inside the service;
- automated nutrition-database lookup or barcode scanning;
- meal planning, medical advice, or health diagnosis;
- multiple users, sharing, billing, or public plugin distribution;
- a browser UI;
- a publicly reachable nginx endpoint.

## 3. Architecture

```text
ChatGPT
   |
   | MCP via OpenAI-hosted tunnel endpoint
   v
OpenAI Secure MCP Tunnel control/data plane
   ^
   | outbound HTTPS long polling
   |
tunnel-client on kage
   |
   | http://127.0.0.1:8787/mcp
   v
mcp-nutrition-db service
   |
   v
/var/lib/mcp-nutrition-db/nutrition.sqlite3
```

The nutrition service and tunnel client are separate systemd services. The MCP
service binds only to `127.0.0.1`. The tunnel client initiates the network path
to OpenAI and forwards requests to the loopback endpoint. No inbound firewall
rule, nginx location, public UUID path, or TLS certificate is needed for MCP.

[OpenAI documents Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
as a private/developer-mode connection for ChatGPT and other supported OpenAI
products. It is not a route for publishing a public plugin; that would require
a stable public HTTPS endpoint and a separate authentication design.

## 4. Technology choices

| Concern | Choice | Rationale |
| --- | --- | --- |
| Runtime | Python 3.13 | Strong SQLite and validation support; straightforward Nix packaging. |
| MCP SDK | Official Python `mcp` SDK with FastMCP | Keeps protocol handling and schemas close to the upstream MCP implementation. |
| Transport | Streamable HTTP at `/mcp` | Supported by the tunnel client and suitable for a long-running service. |
| Validation | Pydantic models | Explicit input/output contracts and useful validation errors. |
| Database | SQLite | A single-owner workload does not justify a network database. Transactions, migrations, WAL, and online backup are sufficient. |
| Tests | pytest | Unit, repository, MCP contract, and integration tests in one ecosystem. |
| Packaging | Nix flake and NixOS module | Matches the existing `flakes` deployment workflow. |

Python and dependency versions will be locked by `flake.lock`. The code should
avoid relying on unreleased SDK behavior.

## 5. MCP API

Tool names are prefixed with `nutrition_` to make their domain and side effects
clear when displayed among other tools. Inputs and outputs use JSON-native
values. Unknown input fields are rejected.

### 5.1 Common concepts

#### Nutrition values

Public values use these units:

- `calories_kcal`
- `protein_g`
- `carbohydrate_g`
- `fat_g`
- `fiber_g`
- `sugar_g`
- `sodium_mg`

All values are optional at the component boundary because an estimate may be
incomplete. Supplied values must be finite and non-negative. An explicit zero
is distinct from an unknown value. Entry and summary totals preserve that
distinction by reporting both values and data-completeness metadata.

#### Time

Inputs use RFC 3339 timestamps with an explicit offset. The service stores the
instant in UTC plus the supplied IANA timezone for calendar grouping. If the
caller omits a timezone, `Europe/Zurich` is used. The service must not inherit
`kage`'s host timezone (`Asia/Tokyo`). Date-range queries use half-open
intervals: `start <= occurred_at < end`.

#### Mutation safety

- Create calls accept an `idempotency_key`; replaying the same key and payload
  returns the original result.
- Updates and deletes require `expected_revision`.
- A stale revision fails with a structured conflict response containing the
  current revision, rather than silently overwriting newer information.
- Every update and delete includes a short `reason` and records an audit row.

### 5.2 `nutrition_log_entry`

Creates a meal or snack.

Required input:

- `idempotency_key`: caller-generated opaque string;
- `occurred_at`: RFC 3339 timestamp;
- `kind`: `breakfast`, `lunch`, `dinner`, `snack`, or `other`;
- `title`: concise human-readable description;
- `components`: one or more complete component estimates.

Optional input:

- `timezone`: IANA timezone, default `Europe/Zurich`;
- `notes`: facts supplied by the user;
- `estimation`: confidence, assumptions, and source description.

Output returns the canonical entry, including its generated `entry_id`,
`revision = 1`, server-derived totals, and completeness metadata.

### 5.3 `nutrition_get_entry`

Fetches one entry by `entry_id`. The response includes components, totals,
estimation metadata, current revision, and timestamps. Deleted entries are not
returned unless a future administrative API explicitly supports that behavior.

### 5.4 `nutrition_update_entry`

Corrects an existing entry. Required input is `entry_id`, `expected_revision`,
`reason`, and at least one changed field. Editable fields are `occurred_at`,
`timezone`, `kind`, `title`, `notes`, `components`, and `estimation`.

If `components` is supplied it replaces the full component list. This avoids
fragile positional patches and lets ChatGPT submit a newly coherent estimate.
The service recalculates totals and increments the revision atomically.

### 5.5 `nutrition_delete_entry`

Soft-deletes an entry using `entry_id`, `expected_revision`, and `reason`.
Deletion is auditable and excluded from normal get, list, and summary calls.
There is no purge tool in the first release.

This tool must be annotated as destructive. Create and update tools must be
annotated as mutating; read tools must be annotated read-only. Tool metadata
should not claim idempotence except where the actual contract guarantees it.

### 5.6 `nutrition_list_entries`

Returns entries within a required bounded time window. Optional filters are
`kind`; pagination uses an opaque cursor and a constrained `limit`. Results are
ordered by `occurred_at` descending, then `entry_id` descending for stable
pagination.

List results include entry totals and concise metadata but may omit components.
The caller uses `nutrition_get_entry` when it needs full detail.

### 5.7 `nutrition_summarize`

Aggregates a bounded time window in a requested timezone. Supported grouping is
`day` or `whole_range`. The response contains:

- summed macro values;
- entry count;
- values averaged per represented day when requested;
- completeness counts for each macro;
- the effective goal and progress for each day when a goal exists.

Unknown component values must not be silently treated as known zeroes. A total
may still be returned, but its completeness field states how many included
entries had known values.

### 5.8 `nutrition_set_goals`

Creates or replaces a goal version effective on a calendar date. Input includes
`effective_from`, `timezone`, and at least one macro target. Targets use the same
public units as nutrition values and must be positive.

Goals are effective-dated, not mutated in place, so historical summaries use
the goal that applied on that day. Setting the same effective date replaces
that version in one transaction and records the change.

### 5.9 `nutrition_get_goals`

With `on_date`, returns the goal version effective on that date. Without it,
returns the current goal and the ordered goal history. The response distinguishes
an unset macro target from zero.

## 6. Canonical entry model

```json
{
  "entry_id": "019...",
  "revision": 2,
  "occurred_at": "2026-08-27T12:30:00+02:00",
  "timezone": "Europe/Zurich",
  "kind": "lunch",
  "title": "Rice bowl with salmon",
  "notes": "User later clarified that the bowl contained 180 g cooked rice.",
  "components": [
    {
      "component_id": "019...",
      "name": "Cooked rice",
      "quantity": 180,
      "unit": "g",
      "portion_notes": "Amount confirmed by user",
      "nutrition": {
        "calories_kcal": 234,
        "protein_g": 4.3,
        "carbohydrate_g": 51.5,
        "fat_g": 0.5,
        "fiber_g": 0.7,
        "sugar_g": 0.1,
        "sodium_mg": 2
      }
    }
  ],
  "totals": {},
  "completeness": {},
  "estimation": {
    "confidence": "medium",
    "assumptions": ["Salmon cooking oil was not visible"],
    "source": "meal_photo_and_user_clarification"
  },
  "created_at": "2026-08-27T10:35:00Z",
  "updated_at": "2026-08-27T10:42:00Z"
}
```

`totals` and `completeness` are output-only and calculated by the server.
Components may describe individual foods or, when detail is unavailable, one
aggregate component for the whole meal. Stable UUIDv7-style identifiers are
preferred for entries and components because they are unique and time-sortable;
the exact library choice is an implementation detail covered by tests.

## 7. Database design

SQLite runs with foreign keys enabled, WAL journaling, and a busy timeout. Every
write operation is transactional. Schema changes use explicit numbered
migrations applied before the service starts accepting requests.

### Tables

#### `entries`

Current entry metadata: identifier, revision, occurrence time in UTC, timezone,
kind, title, notes, estimation metadata, creation/update timestamps, and optional
soft-deletion timestamp.

#### `entry_components`

Current ordered component list. It references `entries` with a foreign key and
stores quantity, unit, portion notes, and optional nutrition values.

#### `entry_revisions`

An immutable JSON snapshot of the canonical entry before each update or delete,
plus the resulting revision, reason, operation, and timestamp. This favors
simple reliable restoration and audit inspection over a complex per-field diff.

#### `daily_goals`

Effective date, timezone, macro targets, and audit timestamps. The pair
`(effective_from, timezone)` is unique.

#### `goal_revisions`

Immutable snapshots of replaced goal versions with reason and timestamp.

#### `idempotency_keys`

Key, operation, normalized request digest, stored response identifier, and
creation time. Reuse with a different payload is an error. Old rows may be
pruned only after a documented retention period; the first release retains
them indefinitely because volume is negligible.

#### `schema_migrations`

Applied migration number and timestamp.

### Numeric representation

The API uses decimal JSON numbers. The database stores scaled integers to avoid
binary floating-point drift: energy in milli-kilocalories and all mass values
in milligrams. Conversion and rounding happen at the repository boundary using
a documented half-up policy. Inputs have sensible precision and magnitude
limits to reject accidental extreme values.

### Indexes

- active entries by `(occurred_at_utc, entry_id)`;
- entries by `(kind, occurred_at_utc)`;
- components by `(entry_id, position)`;
- goals by `(timezone, effective_from)`;
- unique idempotency key.

## 8. Errors and observability

Expected failures return concise structured errors suitable for an agent:

- validation error with field locations;
- entry or goal not found;
- revision conflict with current revision;
- idempotency-key payload mismatch;
- invalid or excessive query window;
- internal database error with a correlation ID but no sensitive details.

Logs are structured JSON to journald and include operation, duration, outcome,
correlation ID, and entry ID where relevant. They must not include full notes,
component payloads, API keys, tunnel credentials, or raw MCP bodies. Health
checks expose process/database readiness only on loopback.

The first release does not require a metrics backend. systemd status, journald,
the MCP health endpoint, and the tunnel client's health listener are sufficient.

## 9. Security and privacy

This database contains sensitive personal dietary information.

- The MCP listener binds only to loopback.
- The tunnel client is the sole network bridge and makes outbound HTTPS
  connections to OpenAI.
- The MCP service uses `noauth` locally because loopback plus the private tunnel
  is the access boundary for this single-owner deployment.
- The OpenAI API key is read from the existing sops secret
  `mcp-nutrition-db-tunnel-apikey` through a systemd credential file. It never
  appears in a Nix store path, command line, environment dump, or repository.
- Local development may use the ephemeral repository-root file `./api-key`,
  which is gitignored. It is only for direct local tunnel-client tests, must be
  passed using the client's file/credential mechanism, and must never be copied
  into Nix configuration, fixtures, snapshots, logs, or production. It will be
  revoked and removed after development.
- The tunnel ID is configuration metadata, not a credential, and may remain as
  a literal in the private `flakes` repository.
- The SQLite database and backups must be readable only by the service account
  and the backup operator.
- Photo bytes remain in ChatGPT and are not copied into this service.
- Logs minimize nutrition content and never log secrets.

If the service becomes multi-user or publicly reachable, this trust model no
longer applies. That version requires per-user identity and authorization,
OAuth 2.1 for MCP, tenant-scoped queries, and a separate threat model.

## 10. Nix flake interface

This repository will expose:

- `packages.<system>.default`: the runnable Python application;
- `apps.<system>.default`: a local development entry point;
- `checks.<system>`: formatting, unit, integration, and package checks;
- `devShells.<system>.default`: development and test tools;
- `nixosModules.default`: the nutrition service module.

The NixOS module will define at least:

```nix
services.mcp-nutrition-db = {
  enable = true;
  package = inputs.mcp-nutrition-db.packages.${pkgs.system}.default;
  listenAddress = "127.0.0.1";
  port = 8787;
  defaultTimezone = "Europe/Zurich";
  stateDirectory = "mcp-nutrition-db";
};
```

Its systemd unit uses a dedicated unprivileged identity or `DynamicUser` with
`StateDirectory=mcp-nutrition-db`, starts after migrations succeed, restarts on
failure, and applies normal hardening such as `NoNewPrivileges`, `PrivateTmp`,
`ProtectSystem=strict`, and a narrow writable state directory.

## 11. `kage` deployment

The deployment repository adds this flake and the independently maintained
[`openai-secure-tunnel-nix`](https://github.com/nakasyou/openai-secure-tunnel-nix)
flake as inputs, both following its `nixpkgs` where supported. `kage` imports the
nutrition module and the tunnel-client module.

The intended tunnel configuration is structurally:

```nix
services.openai-tunnel-client.instances.nutrition = {
  enable = true;
  apiKeyFile = config.sops.secrets.mcp-nutrition-db-tunnel-apikey.path;
  settings = {
    config_version = 1;
    control_plane.tunnel_id = mcpNutritionDbOaiTunnelID;
    health.listen_addr = "127.0.0.1:8788";
    admin_ui.open_browser = false;
    log = {
      level = "info";
      format = "json";
    };
    mcp.server_urls = [{
      channel = "main";
      url = "http://127.0.0.1:8787/mcp";
    }];
  };
};
```

The tunnel unit must order itself after and require the nutrition service. The
secret is already declared in `nixos/kage/configuration.nix`, and the tunnel ID
is already present as non-secret configuration in `nixos/kage/networking.nix`.
Exact placement may be refactored when the module is integrated.

`openai-secure-tunnel-nix` is a small third-party wrapper rather than an OpenAI
project. The flake lock must pin it, CI/evaluation must build its package, and
upstream tunnel-client releases should be reviewed periodically. If it becomes
unmaintained or incompatible, package OpenAI's upstream client directly without
changing the service architecture.

The existing nginx site remains unchanged. The earlier difficult-to-guess URL
design is superseded by Secure MCP Tunnel and must not be implemented in
parallel.

## 12. Backup and recovery

- Create backups with SQLite's online backup mechanism, never by copying a live
  database file without its WAL state.
- Include the state directory in the existing host backup workflow after a
  verified online snapshot is produced.
- Retain multiple generations according to the host's backup policy.
- Document and test a restore into a temporary state directory before the first
  production release.
- A restore replaces state only while the service and tunnel client are stopped,
  after preserving the current database as a recoverable file.

## 13. Testing strategy

- Model tests cover units, boundaries, unknown values, timestamps, and invalid
  inputs.
- Repository tests cover migrations, transactions, idempotency, optimistic
  concurrency, soft deletion, effective-dated goals, and audit snapshots.
- Tool tests assert MCP schemas, annotations, success output, and agent-usable
  errors.
- Summary tests cover timezone boundaries, daylight-saving transitions,
  completeness, and goal selection.
- Integration tests start the Streamable HTTP server and exercise it with an MCP
  client.
- Nix checks build the package and evaluate a minimal NixOS configuration.
- Deployment validation calls all tools through ChatGPT and the actual tunnel,
  including a create-correct-query-delete lifecycle.
- Local tunnel integration may use `./api-key`; tests must skip with a clear
  message when it is absent and must never read or echo its contents in test
  output. Automated CI must not depend on this file.

## 14. Decision log

| Date | Decision | Consequence |
| --- | --- | --- |
| 2026-08-27 | ChatGPT performs image interpretation; the service accepts structured estimates. | No image storage or vision dependency is needed. |
| 2026-08-27 | Use SQLite with scaled integer values. | Simple operations and exact aggregation; one active writer deployment is assumed. |
| 2026-08-27 | Use complete component-list replacement for entry corrections. | Updates are easy for an agent to reason about and audit. |
| 2026-08-27 | Use revisions, idempotency keys, and immutable snapshots. | Retries and conversational corrections do not silently corrupt records. |
| 2026-08-27 | Use OpenAI Secure MCP Tunnel instead of nginx and a secret URL. | No public listener is needed; use is private/developer-mode only. |
| 2026-08-27 | Keep tunnel lifecycle in the deployment repo, separate from the application module. | The application flake remains reusable and does not own OpenAI credentials. |
| 2026-08-27 | Default calendar behavior to `Europe/Zurich`. | Results match the user's intended day rather than the server's Tokyo timezone. |
