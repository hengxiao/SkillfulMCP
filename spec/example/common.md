# example/common/

Framework-agnostic utilities. Everything here is usable from any runner
and has zero framework SDK imports.

## Module map

| File                    | Summary                                             |
| ----------------------- | --------------------------------------------------- |
| `__init__.py`           | Re-exports the public functions                     |
| `mcp_bootstrap.py`      | Catalog provisioning, token minting, skill loading  |
| `skill_dispatcher.py`   | Simulated skill responses (demo default)            |

## `mcp_bootstrap.py`

### `load_network_config(path=None) -> dict`

Reads a `network.yaml`. Defaults to the one at the repo root of `example/`.

### `admin_headers(admin_key) -> dict`

Just `{"X-Admin-Key": admin_key}`.

### `bootstrap_mcp(config, server_url, admin_key) -> None`

Idempotent provisioning. Walks the config:

1. For each skillset: `PUT /skillsets/{id}` (upsert).
2. For each skill in the skillset: `POST /skills`; accept 409 (already
   exists). `PUT /skillsets/{id}/skills/{skill_id}` to associate.
3. For each agent: `POST /agents`; accept 409.

Uses blocking `httpx.Client` — runners call this once at startup, not in
the hot path.

### `get_agent_token(server_url, agent_id, admin_key) -> str`

`POST /token` → return `access_token`. Synchronous.

### `load_agent_skills(server_url, token) -> list[dict]`

`GET /skills` with the bearer token — returns the skills the JWT
authorizes.

### `orchestrator_routing_tool_schema(worker_ids) -> dict`

Framework-agnostic shape for the `route_to_agent` meta-tool:

```python
{
    "name": "route_to_agent",
    "description": "Delegate ... after classifying intent.",
    "input_schema": {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "enum": worker_ids, ...},
            "request_summary": {"type": "string", ...},
        },
        "required": ["agent_id", "request_summary"],
    },
}
```

Each `SkillfulXxxAgent.bind_extra_tool(schema, handler)` translates this
into the framework's native tool format. The handler receives
`(tool_name, args) -> dict` and is called when the model emits the tool.

## `skill_dispatcher.py`

### `dispatch_skill(name, args) -> dict`

Canned responses for `classify_intent`, `lookup_invoice`, `apply_credit`,
`run_diagnostic`, `schedule_technician`. Unknown names return a trivial
`{"result": "ok", "tool": name}`.

Used as the default `on_skill_call` hook by every `Skillful*` agent class.
Override by passing `on_skill_call=my_real_handler` to the constructor.

### `_infer_intent(message) -> str`

Keyword-based classifier for the `classify_intent` skill. Returns one of
`billing`, `technical-support`, `account`, `general`. Used only by
`dispatch_skill`.

## Future work

- Replace `dispatch_skill` with a real registry keyed by skill `executor_url`
  or `executor_mcp` metadata (productization §3.4).
- Make `bootstrap_mcp` async to match the Web UI / framework clients.
- Add retries / backoff — a transient 5xx from the catalog during startup
  currently tears the runner down.
