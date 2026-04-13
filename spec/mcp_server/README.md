# spec/mcp_server — Catalog Server Submodule Specs

Per-file specifications for the FastAPI catalog server under `mcp_server/`.
These are implementation-level docs; the high-level design lives in
[`../architecture.md`](../architecture.md) and [`../prototype.md`](../prototype.md).

## Module map

| File                          | Spec                                                    | Purpose                                                      |
| ----------------------------- | ------------------------------------------------------- | ------------------------------------------------------------ |
| `main.py`                     | [main.md](main.md)                                      | FastAPI app factory, lifespan, router mounting               |
| `config.py`                   | [config.md](config.md)                                  | `Settings` class and `get_settings()` (cached)               |
| `database.py`                 | [database.md](database.md)                              | Engine creation, session factory, DDL bootstrap              |
| `dependencies.py`             | [dependencies.md](dependencies.md)                      | Request-scoped deps (`get_db`, `get_current_claims`, `require_admin`) |
| `auth.py`                     | [auth.md](auth.md)                                      | JWT issuance and validation                                  |
| `authorization.py`            | [authorization.md](authorization.md)                    | Token-claim → allowed-skill-id resolution                    |
| `models.py`                   | [models.md](models.md)                                  | SQLAlchemy ORM tables                                        |
| `schemas.py`                  | [schemas.md](schemas.md)                                | Pydantic request / response schemas                          |
| `catalog.py`                  | [catalog.md](catalog.md)                                | Skill + skillset CRUD and membership                         |
| `registry.py`                 | [registry.md](registry.md)                              | Agent CRUD                                                   |
| `bundles.py`                  | [bundles.md](bundles.md)                                | Archive extraction + bundle storage                          |
| `logging_config.py`           | [logging_config.md](logging_config.md)                  | JSON formatter + request-id context                          |
| `middleware.py`               | [middleware.md](middleware.md)                          | Request-ID, request-size, rate-limit middleware              |
| `ratelimit.py`                | [ratelimit.md](ratelimit.md)                            | Token-bucket rate limiter (backing store for the middleware) |
| `errors.py`                   | [errors.md](errors.md)                                  | Global exception handlers + typed error envelope             |
| `routers/`                    | [routers/README.md](routers/README.md)                  | HTTP route modules (one per resource)                        |

## Conventions used across the module

- **Every write endpoint requires `X-Admin-Key`**, validated by `require_admin`. Every skill-delivery read endpoint requires a bearer JWT, validated by `get_current_claims`. The `/admin/*` router pairs the read endpoints behind admin-key for the Web UI.
- **Error shape.** HTTP errors are raised via `HTTPException(status_code, detail=…)`. 404 for missing resources, 409 for uniqueness violations, 401 for JWT failures, 403 for auth-header failures, 204 for successful deletes, 201 for created resources.
- **Session lifecycle.** `get_db` opens a session per request, yields, then closes. No long-lived sessions; commits happen inside the service functions (`catalog.py`, `registry.py`).
- **Cascade handling.** SQLite doesn't enforce FKs by default and the service layer uses bulk deletes (`db.query(...).delete()`), so ORM-level `cascade` is bypassed. Functions that delete parent rows explicitly delete dependents first (see `catalog.delete_skill_*`).
