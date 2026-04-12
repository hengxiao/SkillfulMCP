# mcp_server/database.py

Engine + session factory + DDL bootstrap.

## Public functions

### `make_engine(url: str) -> Engine`

Creates a SQLAlchemy engine. Special-cases `sqlite:///:memory:` to use
`StaticPool` + `check_same_thread=False` so that a single in-memory DB is
shared across all connections within a process. That's what lets the test
suite use `:memory:` and still see writes across request handlers.

Non-memory SQLite URLs use a default pool + `check_same_thread=False`. Other
URLs (Postgres, etc.) use `create_engine`'s defaults.

### `make_session_factory(engine) -> sessionmaker`

Returns a `sessionmaker` configured with `autocommit=False`, `autoflush=False`.
Autoflush off is deliberate — the service layer calls `db.flush()` / `db.commit()` at
well-defined points so we can reason about ordering (see the `_refresh_is_latest`
sequence in `catalog.py`).

### `init_db(url: str) -> sessionmaker`

Convenience: `create_engine` → `Base.metadata.create_all(engine)` → return
session factory. Called once per app instance from the lifespan handler.

## Schema bootstrap

`Base.metadata.create_all(engine)` is idempotent — adds missing tables, leaves
existing ones alone. For the prototype that's enough; for production we need
Alembic (see `productization.md` §3.2).

## SQLite foreign keys

SQLite has FKs off by default. The prototype uses **bulk query deletes**
(`db.query(...).delete()`) which bypass ORM cascades anyway, so the
service functions explicitly delete child rows. Moving to Postgres (planned)
makes DB-level cascades reliable.

## Testing

`tests/conftest.py::db_session` creates a fresh engine per test:

```python
engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
Base.metadata.create_all(engine)
```

Each test gets an isolated DB; teardown calls `drop_all`.

## Future work

- Replace `create_all` with Alembic migrations.
- Enable `PRAGMA foreign_keys = ON` for SQLite via a connect listener so
  `ondelete=CASCADE` works in dev.
- Add read-replica session factory for scale-out reads
  (`productization.md` §3.2).
- Add connection pool metrics (checkouts, waits).
