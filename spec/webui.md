# Web UI Spec

This document describes the browser-based management interface for the SkillfulMCP server.

## Goals

- Let operators add, edit, and delete skills and skillsets without touching the CLI or raw HTTP.
- Show version history for skills and allow per-version management.
- Show and manage the skill membership of each skillset.

## Stack

| Layer | Choice | Reason |
|---|---|---|
| Web server | FastAPI (Python) | Consistent with the rest of the project; async-native |
| Templating | Jinja2 | Server-side HTML; no build step |
| Interactivity | HTMX 1.9 | Inline deletes and dynamic form swaps without a JS framework |
| Styling | Bootstrap 5.3 (CDN) | Rapid layout; no build step |
| HTTP client | httpx (async) | Calls the MCP server API; same library already used in the CLI |

The web UI is a **standalone FastAPI process** that proxies all data operations to the MCP server API. It does not touch the database directly.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `MCP_SERVER_URL` | `http://localhost:8000` | Base URL of the MCP server |
| `MCP_ADMIN_KEY` | _(empty)_ | Admin key forwarded as `X-Admin-Key` on every proxied request |
| `WEBUI_HOST` | `127.0.0.1` | Bind host for the web UI server |
| `WEBUI_PORT` | `8080` | Port for the web UI server |

The web UI reuses the same `.env` file as the MCP server.

## MCP Server additions

The existing `GET /skills` and `GET /skillsets/{id}/skills` endpoints require a Bearer JWT (agent token), which the web UI does not hold. A new `/admin` router is added to the MCP server to expose admin-key-protected equivalents:

| Endpoint | Description |
|---|---|
| `GET /admin/skills` | Latest version of every skill |
| `GET /admin/skills/{skill_id}` | Latest version of one skill |
| `GET /admin/skills/{skill_id}/versions` | All versions of one skill |
| `GET /admin/skillsets/{skillset_id}/skills` | All skills in a skillset (no JWT) |

## Pages

### Dashboard `/`

- Three stat cards: total skillsets, total skills, total agents.
- Each card links to the corresponding list page.

### Skillsets `/skillsets`

- Table: id · name · description · created date · actions (Detail, Delete).
- Delete uses HTMX `hx-delete` with confirmation; removes the row on success.
- "New Skillset" card below the table with fields: id, name, description.
- Submits via standard POST → redirect (PRG pattern) with flash message.

### Skillset detail `/skillsets/{id}`

- Info card showing id, created, updated timestamps.
- Inline edit form for name and description; POST to `/skillsets/{id}/update` → redirect.
- Table of skills currently in the skillset (id, name, latest version).
  - "Remove" button per row — HTMX `hx-delete` removes the association and the row.
- "Add Skill" card with a `<select>` of all catalog skills not yet in the skillset;
  POST to `/skillsets/{id}/skills` → redirect.

### Skills `/skills`

- Table: id · name · latest version badge · description · actions (Detail, Delete All).
- Delete All uses HTMX `hx-delete` with confirmation.
- "New Skill" card with fields: id, name, description, version, metadata (JSON textarea), skillset associations (checkboxes).

### Skill detail `/skills/{id}`

- Info card: id, latest version badge.
- Edit form for name, description, and metadata of the latest version;
  POST to `/skills/{id}/update` → redirect.
- Version history table: version · is_latest badge · created date · Delete Version button.
  - Delete Version uses HTMX `hx-delete`; removes the row.
- "Add Version" card with fields: version, name, description, metadata.

## Interaction patterns

| Operation | Mechanism | On success |
|---|---|---|
| Create (skillset, skill) | Standard form POST → HTTP 303 redirect | Full page reload with flash message |
| Update | Standard form POST → HTTP 303 redirect | Full page reload with flash message |
| Delete row (HTMX) | `hx-delete` + `hx-confirm` + `hx-target="closest tr"` + `hx-swap="delete"` | Row removed from DOM; no page reload |
| Add skill to skillset | Standard form POST → HTTP 303 redirect | Full page reload with flash message |
| Remove skill from skillset | `hx-delete` + `hx-confirm` + `hx-target="closest tr"` + `hx-swap="delete"` | Row removed from DOM |

Flash messages are passed as `?msg=...&msg_type=success|error` query parameters and rendered once by the base template.

## Error handling

All proxied API calls are wrapped in try/except. On `httpx.HTTPStatusError`, the detail field from the JSON response is extracted and shown as an error flash. On network error, a generic "Could not reach MCP server" message is shown.

## Running

```bash
# Start the MCP server first
MCP_JWT_SECRET=secret MCP_ADMIN_KEY=admin-key mcp-server

# Then start the web UI
MCP_ADMIN_KEY=admin-key webui-server
# or
MCP_ADMIN_KEY=admin-key uvicorn "webui.main:create_app" --factory --port 8080 --reload
```
