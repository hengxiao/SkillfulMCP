# mcp_server/main.py

FastAPI application factory and lifespan management.

## Public API

### `create_app(database_url: str | None = None) -> FastAPI`

Construct and return the FastAPI app. The `database_url` override is test-only; production reads it from settings.

### `run() -> None`

Thin entry point used by the `mcp-server` console script. Runs uvicorn with
`reload=True`, bound to `0.0.0.0:8000`. Production deployments should call
`uvicorn` directly with proper worker counts (see
[`../productization.md`](../productization.md)).

## Lifespan

`@asynccontextmanager lifespan`:

1. `get_settings()` fetches the validated `Settings` (throws at startup if `MCP_JWT_SECRET` is unset).
2. `database_url = override or settings.database_url`.
3. `init_db(url)` creates the engine, runs `Base.metadata.create_all()`, returns a session factory.
4. Session factory is stashed on `app.state.session_factory` so `get_db` (a request-scoped dep) can pull it without a module-level global.

No shutdown actions currently — SQLite doesn't need explicit disposal, and no background tasks are running.

## Router mounting

Ordered intentionally for route-matching clarity; FastAPI uses first-match semantics and some prefixes overlap at deeper segments.

| Router              | Prefix         | Auth                    |
| ------------------- | -------------- | ----------------------- |
| `health`            | (none)         | Public                  |
| `token`             | `/token`       | Admin key               |
| `skills`            | `/skills`      | Mixed (read: JWT, write: admin) |
| `bundles`           | `/skills`      | Mixed (read: JWT, write: admin) |
| `agents`            | `/agents`      | Admin key               |
| `skillsets`         | `/skillsets`   | Mixed (read varies)     |
| `admin`             | `/admin`       | Admin key               |

The `bundles` router is mounted **after** `skills` so deeper bundle subpaths don't mask the shorter `skills` routes. In practice FastAPI picks the most specific match regardless, but ordering keeps OpenAPI docs readable.

## Failure modes

- **Missing `MCP_JWT_SECRET`** — lifespan calls `get_settings()`; `Settings.__init__` raises `RuntimeError`, which makes the app fail to start. Intentional: we'd rather crash at boot than silently start signing tokens with an empty key.
- **Database URL pointing at an unreachable server** — `create_engine` returns lazily; failure surfaces on the first query. For a production system, add a readiness probe that actually pings the DB (see `productization.md` §3.6).

## Testing

Fixture `conftest.py::client` calls `create_app(database_url="sqlite:///:memory:")` and wraps it in a `TestClient`. Every test gets an isolated DB.

## Future work

- Split `run()` into a proper CLI entry that respects worker count, bind host, TLS config, and log format from env.
- Add `on_startup` check for the admin key presence in non-dev environments.
- Add OpenTelemetry instrumentation here (see productization plan).
