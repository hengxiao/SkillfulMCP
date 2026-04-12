# webui/client.py

Async `httpx` wrapper around the MCP catalog API. Every Web UI request
handler delegates to methods on this class.

## `MCPError(Exception)`

Carries the server's error detail plus the HTTP status code. Raised by
`MCPClient._request` on any non-2xx response. Route handlers catch it and
convert it into a redirect with `msg_type=error` or a rendered error page.

## `MCPClient`

Reads `settings.mcp_server_url` and `settings.admin_key` on construction.
Every outgoing request carries `X-Admin-Key`. `httpx.AsyncClient` is
constructed per request (keeps the Web UI stateless; connection reuse would
need a shared client and lifecycle hooks).

### `_request(method, path, **kwargs)`

Central HTTP helper. Handles:
- `httpx.HTTPStatusError` → extracts `detail` from JSON body if present, else falls back to raw text → `MCPError`.
- `httpx.RequestError` (DNS, connection refused, timeout) → `MCPError("Could not reach MCP server …")`.
- `204 No Content` → returns `None`.
- Otherwise returns the parsed JSON body.

### Skillset methods

- `list_skillsets()` → `GET /skillsets`
- `get_skillset(id)` → `GET /skillsets/{id}`
- `create_skillset(data)` → `POST /skillsets`
- `update_skillset(id, data)` → `PUT /skillsets/{id}`
- `delete_skillset(id)` → `DELETE /skillsets/{id}`
- `list_skillset_skills(id)` → `GET /admin/skillsets/{id}/skills` (admin endpoint; no JWT needed)
- `associate_skill(skillset_id, skill_id)` / `disassociate_skill(...)` — PUT / DELETE on the association path.

### Skill methods

- `list_skills()` → `GET /admin/skills`
- `get_skill(id, version=None)` → `GET /admin/skills/{id}[?version=...]`
- `list_skill_versions(id)` → `GET /admin/skills/{id}/versions`
- `create_skill(data)` → `POST /skills`
- `update_skill(id, data)` → `PUT /skills/{id}`
- `delete_skill(id, version=None)` → `DELETE /skills/{id}[?version=...]`

### Bundle methods

- `list_bundle_files(id, version)` → `GET /admin/skills/{id}/versions/{v}/files`
- `get_bundle_file(id, version, path)` → `GET /admin/skills/{id}/versions/{v}/files/{path}` — returns raw `bytes` (separate code path because the response isn't JSON).
- `upload_bundle(id, version, filename, data)` → `POST /skills/{id}/versions/{v}/bundle` as multipart. Returns `BundleUploadResponse` JSON.
- `delete_bundle(id, version)` → `DELETE /skills/{id}/versions/{v}/bundle`.
- `copy_bundle(dst_skill, dst_version, src_skill, src_version)` → `POST /skills/{dst}/versions/{dv}/bundle/copy-from/{src}/{sv}`.

### Agent methods

- `list_agents()` → `GET /agents` (used only for the dashboard count).

## Separation of concerns

The MCPClient owns no business rules. Validation, flash-message wording,
redirect URLs, and partial-failure behavior all live in `main.py`; this
module is the thinnest practical HTTP client.

## Testing

Covered by `tests/test_webui.py` via `AsyncMock(MCPClient)`. No live tests —
if the MCP server API changes shape, those contract failures surface in the
catalog's own test suite.

## Future work

- Reuse a single `httpx.AsyncClient` per process (app state).
- Retry + exponential backoff on transient 5xx / network errors.
- Per-call timeout override (currently one hard-coded 10s except for bundle
  I/O which bumps to 30–60s).
- Propagate the operator's bearer token once the Web UI stops using the
  shared admin key (productization §3.5).
