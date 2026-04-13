# mcp_server/routers/admin.py

Admin-key-gated **read** endpoints used by the Web UI. Mirrors the
JWT-scoped read endpoints without requiring the Web UI to hold a JWT of
its own.

## Endpoints

| Method | Path                                                                 | Returns                     |
| ------ | -------------------------------------------------------------------- | --------------------------- |
| GET    | `/admin/skills`                                                      | `list[SkillResponse]` — latest version of every skill |
| GET    | `/admin/skills/{skill_id}?version=`                                  | `SkillResponse` — latest, or a specific version |
| GET    | `/admin/skills/{skill_id}/versions`                                  | `list[SkillVersionInfo]`    |
| GET    | `/admin/skillsets/{skillset_id}/skills`                              | `list[SkillResponse]` — all skills in the skillset (no JWT filter) |
| GET    | `/admin/skills/{skill_id}/versions/{version}/files`                  | `list[BundleFileInfoResponse]` |
| GET    | `/admin/skills/{skill_id}/versions/{version}/files/{path:path}`      | Raw file bytes with `X-Content-SHA256` header |
| POST   | `/admin/tokens/revoke`                                               | `{jti}` → 204. Revoke a token (Wave 4).       |
| GET    | `/admin/tokens/revoked-count`                                        | `{count}` — current deny-list size.           |

## Why it exists

The Web UI proxies all data access to the MCP server but doesn't hold a
per-agent JWT. Exposing the JWT-scoped reads behind admin key lets the Web
UI show the full catalog (not just one agent's slice) without synthesizing
bearer tokens.

## Security consequence

The admin key grants unrestricted catalog read + write across all skills and
agents. Protect accordingly. This router is the primary reason the
productization plan flags "replace admin key with operator OIDC" as P0.

## What this router does **not** expose

- Write endpoints. Those live in `skills.py`, `skillsets.py`, `agents.py`, `bundles.py`, and already accept admin key.
- Agent CRUD. `routers/agents.py` is already admin-gated.
- Token minting. `/token` is separate and admin-gated.

## Testing

Covered transitively by `tests/test_api_*.py` (any endpoint called with
`ADMIN_HEADERS`) and by `tests/test_webui.py` via `AsyncMock` of the
`MCPClient`.
