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

Design accepted; implementation has not started. Phase 1 in the implementation
plan is next.
