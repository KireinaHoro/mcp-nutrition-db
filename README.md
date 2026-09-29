# mcp-nutrition-db

`mcp-nutrition-db` is a private MCP service that lets ChatGPT record, correct,
and query meal nutrition, training sessions, and nutrition goals. ChatGPT
interprets meal photos and conversation context; this service owns the durable,
auditable data and calculates daily energy budgets.

The production service is intended to run on the NixOS host `kage`. It listens
only on loopback and is reached from ChatGPT through OpenAI Secure MCP Tunnel,
so it does not require an nginx route or a public MCP endpoint.

## Project documents

- [System design](docs/design.md) defines architecture, module responsibilities,
  persistence guarantees, and operations.
- [Nutrition log API](docs/nutrition-api.md) defines entries, trainings, summaries,
  goals, activity plans, and errors.
- [Fixed food inventory API](docs/food-inventory-api.md) describes reusable
  foods, portion resolution, history linking, and USDA-first sourcing.
- [Implementation plan](docs/implementation-plan.md) is the source of truth for
  phases, acceptance criteria, progress, and deferred work.
- [Exercise and recovery energy-credit policy](src/mcp_nutrition_db/energy-credit-policy.md)
  defines confidence-adjusted training allowance, non-recurring recovery-day
  credits, bounded surplus repayment, recovery protection, and MCP presentation.
- [Local testing](docs/local-testing.md) explains direct Codex attachment and
  the temporary Secure MCP Tunnel workflow for ChatGPT Web.

The implementation plan should be updated in the same commit as meaningful
implementation milestones. Design changes should be recorded in the decision
log before or alongside the code that depends on them.

## Current status

The service implements schema v5 and twenty-eight MCP tools, including the reusable
food catalog, USDA lookup, portion resolution, and audited history linking. Repository,
schema, calendar, MCP process, Streamable HTTP, package, and NixOS evaluation
checks pass; see the implementation plan for the exact verified state.

Ordinary meal components reference inventory foods and immutable revisions. Search
before creating a food; normalized short names, USDA IDs, and product identifiers
are unique even for archived foods. Historical links preserve existing nutrition
snapshots instead of recalculating old meals.

USDA lookups use a read-only local database built from checksum-pinned official
Foundation, SR Legacy, and FNDDS bulk releases. There are no runtime USDA API
calls, keys, or network fallback. Nix packages include the database; other
installations set `MCP_NUTRITION_USDA_DATABASE` to its path. The NixOS override is
`services.mcp-nutrition-db.usdaDatabase`.

Selected USDA records are copied into immutable evidence snapshots in the
personal database. Dataset updates change available search results, never
existing food revisions or meals. See [offline USDA data](docs/usda-reference.md)
for coverage, provenance, and the explicit update process.

## Development

Enter the pinned environment and run the checks:

```console
nix flake check --print-build-logs
```

For focused iteration, enter `nix develop` and run `PYTHONPATH=src pytest`,
`ruff check src tests`, `ruff format --check src tests`, or `PYTHONPATH=src mypy src`.
These same checks are enforced by the flake and CI.

Run a loopback development server with disposable state:

```console
nix run . -- serve --database /tmp/mcp-nutrition-db.sqlite3
```

Create an atomic SQLite online snapshot without stopping the server:

```console
nix run . -- backup \
  --database /tmp/mcp-nutrition-db.sqlite3 \
  --output /tmp/mcp-nutrition-db.backup.sqlite3
```

The MCP endpoint is `http://127.0.0.1:8787/mcp`; readiness is available at
`http://127.0.0.1:8787/healthz`. The server refuses a non-loopback HTTP bind by
default. See [local testing](docs/local-testing.md) to attach a new Codex session
or run the pinned tunnel client for ChatGPT Web.

Garmin scale/workout import, private sops login, connection hints and explicit
weight-budget reviews are documented in [Garmin sync](docs/garmin-sync.md).
