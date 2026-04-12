# cli/main.py

Typer-based admin CLI. Like the Web UI, every operation goes through the
MCP server HTTP API — the CLI never touches the database directly. Requires
`MCP_SERVER_URL` (default `http://localhost:8000`) and `MCP_ADMIN_KEY` in
the environment.

## Top-level app layout

```
mcp-cli
├── skill
│   ├── add       — create or upsert a skill version
│   └── delete    — delete all versions or one specific version
├── agent
│   ├── add       — register or update an agent
│   └── delete    — remove an agent
├── token
│   └── issue     — mint a JWT for an agent (prints to stdout)
└── catalog
    └── import    — bulk-import a YAML/JSON file of skillsets / skills / agents
```

`--help` is the default when a command group is invoked without a
subcommand (`no_args_is_help=True` on every `Typer(...)`).

## Helpers

- `_base_url()` — reads `MCP_SERVER_URL`.
- `_admin_headers()` — reads `MCP_ADMIN_KEY` → `{"X-Admin-Key": "..."}`.
- `_handle_error(resp)` — if `resp.is_success` is False, prints the JSON
  `detail` field (or raw text) and exits with code 1.

## Commands

### `skill add`

Args:

| Option         | Purpose                                                         |
| -------------- | --------------------------------------------------------------- |
| `--id`         | Required. Skill id.                                             |
| `--name`       | Required. Display name.                                         |
| `--version`    | Required. Semver.                                               |
| `--description`| Optional.                                                       |
| `--skillset`   | Optional. One skillset id to associate with.                    |
| `--metadata`   | Optional JSON string; parsed via `json.loads`.                  |

Behavior: `POST /skills`. On 409 (already exists), upgrades to `PUT /skills/{id}` with the same fields minus `skillset_ids` (upsert does not touch associations).

### `skill delete`

Args: `--id`, `--version` (optional).

Behavior: `DELETE /skills/{id}?version=…`. Delete all versions if `--version` is omitted.

### `agent add`

| Option         | Purpose                                                             |
| -------------- | ------------------------------------------------------------------- |
| `--id`, `--name`| Required.                                                          |
| `--skillsets`  | Comma-separated skillset ids.                                       |
| `--skills`     | Comma-separated explicit skill ids.                                 |
| `--scope`      | Comma-separated scopes (default `read`).                             |

Behavior: `POST /agents` → on 409, `PUT /agents/{id}` with the same payload.

### `agent delete`

`DELETE /agents/{id}`.

### `token issue`

Args: `--agent-id`, `--expires-in` (default 3600 seconds).

Behavior: `POST /token`; prints the raw `access_token` to stdout. Designed
for shell composition — `export MCP_TOKEN=$(mcp-cli token issue --agent-id X)`.

### `catalog import`

Args: `--file` (YAML or JSON), `--upsert` (default off).

File schema:

```yaml
skillsets:
  - { id: ..., name: ..., description: ... }
skills:
  - { id: ..., name: ..., version: ..., description: ..., metadata: {}, skillset_ids: [...] }
agents:
  - { id: ..., name: ..., skillsets: [...], skills: [...], scope: [...] }
```

Behavior: walks the three sections in order (skillsets first so skill →
skillset associations resolve). For each entity, `POST /.../`; on 409 with
`--upsert`, falls back to `PUT`. Without `--upsert`, the first collision
aborts the import.

On success prints summary counts.

## Error handling

Any non-2xx response prints a one-line error to stderr and exits with code
1. This is deliberately minimal — the CLI is a thin scripting surface, not
a replacement for the Web UI.

## Configuration

- `load_dotenv()` runs at import time so the CLI picks up the repo `.env`
  automatically.
- Prints errors via `typer.echo(..., err=True)` so they can be captured in
  scripts.

## Testing

The CLI isn't covered by unit tests — its behavior is a thin function of
the HTTP API, which is tested elsewhere. Integration testing
would need a live server and is tracked as a future task alongside the
productization plan's CI additions.

## Future work

- `skill list`, `agent list`, `skillset list` commands for interactive
  introspection.
- `skill get --id X --version Y` printing metadata + bundle file list.
- Bundle-level commands (`bundle upload`, `bundle download`) to pair with
  the HTTP endpoints.
- Non-zero exit codes differentiated by failure class (auth vs not-found vs
  validation).
- Structured output mode (`--output json`) for scripting.
- Move to operator OIDC instead of raw admin key (productization §3.5).
