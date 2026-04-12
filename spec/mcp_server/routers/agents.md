# mcp_server/routers/agents.py

Agent CRUD. All endpoints are admin-gated — there's no per-agent
self-service in the prototype.

## Endpoints

| Method | Path              | Returns                 | Notes                                  |
| ------ | ----------------- | ----------------------- | -------------------------------------- |
| GET    | `/agents`         | `list[AgentResponse]`   | All agents. No pagination.             |
| GET    | `/agents/{id}`    | `AgentResponse`         | 404 if missing.                         |
| POST   | `/agents`         | `AgentResponse`, 201    | 409 on duplicate id.                   |
| PUT    | `/agents/{id}`    | `AgentResponse`         | Partial update (`None` = leave alone). 404 if missing. |
| DELETE | `/agents/{id}`    | 204                     | 404 if missing.                        |

## Scope validation

`AgentCreate` / `AgentUpdate` validate `scope` against `VALID_SCOPES`
(`{"read", "execute"}`) at the schema layer. Unknown scopes → 422.

## Interaction with `/token`

An agent registered here can immediately mint tokens via `POST /token`.
Editing `skillsets` / `skills` / `scope` on the agent does **not** invalidate
existing tokens — current tokens carry baked-in claims until their `exp`.

## Testing

`tests/test_api_agents.py` — 18 tests covering:
- CRUD happy paths.
- Duplicate → 409.
- 404 handling.
- Admin-key enforcement.
- Partial update via PUT (each field independently).
- Scope validation (bad value → 422).

## Future work

- Pagination + filtering.
- Agent self-introspection (an authenticated agent can `GET /agents/me`).
- Trigger token revocation when critical fields (scope, skillsets) change.
- Agent provenance fields (`created_by`, `owner_operator_id`) for audit.
