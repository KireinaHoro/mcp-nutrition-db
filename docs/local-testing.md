# Local MCP and tunnel testing

These instructions exercise the same Streamable HTTP endpoint used in
production while keeping the development database, tunnel profile, and API key
out of Git.

## Start the MCP service

From this repository, run:

```console
nix run . -- serve
```

This creates the ignored `nutrition.sqlite3` database and listens only at
`http://127.0.0.1:8787/mcp`. In another terminal, confirm readiness:

```console
curl --fail http://127.0.0.1:8787/healthz
```

The committed `.codex/config.toml` registers that endpoint as the optional
`nutrition` MCP server and asks for approval only for writes. Codex reads MCP
configuration when a client session starts, so open a new Codex session from
this trusted repository after the service is listening. An already-running
session does not dynamically acquire the new tools.

ChatGPT Web does not read `.codex/config.toml`; it reaches the same local server
through Secure MCP Tunnel instead.

## Initialize the local tunnel profile

The project flake pins the tunnel client supplied by
`github:nakasyou/openai-secure-tunnel-nix`. The temporary development key must
be present at `./api-key`; never use that key for the production service.

Set the revealed tunnel identifier in the current shell. The identifier is not
secret, but keeping it out of this repository makes the app flake reusable:

```console
export NUTRITION_TUNNEL_ID=tunnel_replace_me
```

Create an ignored local profile. The profile contains only a reference to the
key file, not the key value:

```console
nix run .#tunnel-client -- init \
  --profile nutrition-local \
  --profile-dir ./.tunnel-client \
  --tunnel-id "$NUTRITION_TUNNEL_ID" \
  --mcp-server-url http://127.0.0.1:8787/mcp \
  --control-plane-api-key-ref "file:$PWD/api-key"
```

Validate local reachability and control-plane configuration:

```console
nix run .#tunnel-client -- doctor \
  --profile nutrition-local \
  --profile-dir ./.tunnel-client \
  --explain
```

Then keep the client running while ChatGPT discovers or invokes tools:

```console
nix run .#tunnel-client -- run \
  --profile nutrition-local \
  --profile-dir ./.tunnel-client
```

Do not enable raw HTTP logging: meal contents and credentials may be present in
protocol traffic. The tunnel client's local status UI is at
`http://127.0.0.1:8080/ui` with the default profile settings.

## Connect ChatGPT

In ChatGPT developer mode, create or refresh the custom MCP app/plugin using
the same tunnel identifier. Both the nutrition service and tunnel-client must
remain running during discovery and tool calls. If app creation previously
failed while they were offline, retry it after `doctor` passes and `run` has
connected.

For the first end-to-end check:

1. Ask ChatGPT to list the available nutrition tools.
2. Log a simple meal, explicitly marking components as estimates.
3. Query today's entries and summary without supplying RFC3339 timestamps.
4. Correct a portion and verify the existing entry revision increases.
5. Set and query a calorie and macro goal.

After tunnel-dependent development is finished, revoke the temporary key and
delete `./api-key` and `./.tunnel-client/`.
