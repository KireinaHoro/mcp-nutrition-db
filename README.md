# mcp-nutrition-db

`mcp-nutrition-db` is a private MCP service that lets ChatGPT record, correct,
and query meal nutrition and nutrition goals. ChatGPT interprets meal photos and
conversation context; this service owns the durable, auditable data.

The production service is intended to run on the NixOS host `kage`. It listens
only on loopback and is reached from ChatGPT through OpenAI Secure MCP Tunnel,
so it does not require an nginx route or a public MCP endpoint.

## Project documents

- [System design](docs/design.md) defines the product boundary, MCP tools, data
  model, security model, and NixOS deployment.
- [Implementation plan](docs/implementation-plan.md) is the source of truth for
  phases, acceptance criteria, progress, and deferred work.

The implementation plan should be updated in the same commit as meaningful
implementation milestones. Design changes should be recorded in the decision
log before or alongside the code that depends on them.

## Current status

The local MVP implements the SQLite schema and all eight MCP tools. Repository,
schema, calendar, and MCP stdio integration tests pass. Streamable HTTP and Nix
closure validation are the next checkpoints; see the implementation plan for
the exact verified state.

## Development

Enter the pinned environment and run the checks:

```console
nix develop
PYTHONPATH=src ruff format --check src tests
PYTHONPATH=src ruff check src tests
PYTHONPATH=src mypy src
PYTHONPATH=src pytest
```

Run a loopback development server with disposable state:

```console
nix run . -- serve --database /tmp/mcp-nutrition-db.sqlite3
```

The MCP endpoint is `http://127.0.0.1:8787/mcp`; readiness is available at
`http://127.0.0.1:8787/healthz`. The server refuses a non-loopback HTTP bind by
default.
