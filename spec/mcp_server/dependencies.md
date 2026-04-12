# mcp_server/dependencies.py

Request-scoped FastAPI dependencies. Every route uses one or more of these.

## `get_db(request: Request) -> Session`

Yields a SQLAlchemy session drawn from `request.app.state.session_factory`.
Closes on generator exit.

**Why per-request**: Sessions are cheap to create, expensive to hold. A
module-level session would require thread/task-locals and complicate cleanup.
FastAPI's generator-dep pattern already handles the lifecycle correctly.

## `get_current_claims(credentials = Depends(HTTPBearer)) -> dict`

Extracts the `Authorization: Bearer <token>` header, validates the JWT via
`auth.validate_token`, and returns the decoded claim dict.

- `HTTPBearer(auto_error=True)` — missing / malformed headers surface as 403
  from FastAPI's security layer before `validate_token` runs.
- Invalid token signature / expired / wrong issuer → 401 (from
  `validate_token`).

## `require_admin(x_admin_key: str = Header(default="")) -> None`

Compares the `X-Admin-Key` request header against `settings.admin_key`.

- If `settings.admin_key` is empty (dev mode), **the check is skipped** and
  any caller passes. Production must set the key; the productization plan
  calls this out as P0.
- Otherwise header must match exactly; mismatch → 403 with a clear detail
  message.

**Not** a bearer token — this is a shared static secret. Covered by every
write endpoint and by the `/admin/*` read endpoints.

## Dependency graph

```
GET /skills                 get_current_claims  + get_db
GET /skills/{id}            get_current_claims  + get_db
GET /admin/skills           require_admin       + get_db
POST /skills                require_admin       + get_db
POST /token                 require_admin       + get_db
POST /agents                require_admin       + get_db
GET /skillsets/{id}/skills  get_current_claims  + get_db
(and so on)
```

## Testing

`tests/conftest.py::ADMIN_HEADERS` + `bearer(token)` helpers wrap the header
construction. Routes tested at the HTTP level go through these deps; unit
tests that exercise only the service layer construct `Session` directly from
the `db_session` fixture.

## Future work

- Replace `require_admin` with operator OIDC (productization §3.1).
- Add `get_request_id` dep for log correlation.
- Add `get_tenant` dep that extracts tenant from operator session / JWT claim
  and passes it through to every service call.
