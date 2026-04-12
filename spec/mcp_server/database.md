# mcp_server/database.py

Engine + session factory + schema bootstrap.

## Public functions

### `make_engine(url: str) -> Engine`

Creates a SQLAlchemy engine. Two special cases:

- **`sqlite:///:memory:`** — uses `StaticPool` + `check_same_thread=False`
  so a single in-memory DB is shared across all connections in a process.
  This is what lets the test suite run with `:memory:` and still observe
  writes across request handlers.
- **Any SQLite URL** — installs a `connect` event listener that executes
  `PRAGMA foreign_keys=ON` on every new connection. SQLite has FK
  enforcement **off** by default, so `ondelete=CASCADE` was previously a
  no-op; enabling it gives dev behavior parity with Postgres.

Other URLs (`postgresql://…`, etc.) use `create_engine`'s defaults.

### `make_session_factory(engine) -> sessionmaker`

`autocommit=False`, `autoflush=False`. The service layer calls `db.flush()`
and `db.commit()` at well-defined points (see `catalog.py` invariants).

### `bootstrap_schema(engine, url) -> None`

Ensures the schema matches `Base.metadata` for the given URL.

- **`sqlite:///:memory:`** → `Base.metadata.create_all(engine)`. Alembic is
  overkill for throwaway in-process DBs; `StaticPool` would also make a
  second alembic connection see a different database.
- **Any other URL** → runs `alembic upgrade head` against the URL. This is
  what production, staging, and on-disk dev SQLite all take. Migrations
  are the source of truth outside test isolation.

The migration runner sets `MCP_DATABASE_URL` in-process so
`migrations/env.py` picks it up the same way the `alembic` CLI does, then
restores the prior value.

### `init_db(url: str) -> sessionmaker`

`make_engine` → `bootstrap_schema` → return session factory. Called from
the lifespan handler in `main.py`.

## Configuration

Supported URLs:

| URL form                           | Requires                 |
| ---------------------------------- | ------------------------ |
| `sqlite:///:memory:`               | nothing (tests)          |
| `sqlite:///path/to/skillful.db`    | nothing                  |
| `postgresql://user:pass@host/db`   | `pip install -e ".[postgres]"` (installs `psycopg2-binary`) |

## Testing

- `tests/conftest.py::client` uses `sqlite:///:memory:`, which hits the
  `create_all` path. Existing 217 tests pass unchanged.
- `tests/test_migrations.py` verifies `alembic upgrade head` builds a
  schema matching `Base.metadata` on SQLite, and round-trips down to
  empty with `downgrade base`. A sibling test runs the same assertions
  against Postgres when `MCP_TEST_POSTGRES_URL` is set, skipped otherwise.

## Future work

- **Read-replica session factory** — `make_session_factory_readonly(url_replica)` for scale-out (productization §3.2 P1).
- **Connection pool metrics** — wrap `QueuePool` in a subclass that emits `checkouts`, `waits`, `overflow` counters to the metrics pipeline.
- **Managed Postgres bootstrap** — add `bootstrap_schema` guard that refuses to run migrations when multiple replicas race at startup (advisory lock on Postgres, file lock on SQLite).
