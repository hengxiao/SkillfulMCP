# Schema Migrations

SkillfulMCP uses [Alembic](https://alembic.sqlalchemy.org/) for schema
migrations. Migrations are the **source of truth** for the database schema
outside test isolation.

## When migrations run

| Context                          | Mechanism                          |
| -------------------------------- | ---------------------------------- |
| In-process tests (`sqlite:///:memory:`) | `Base.metadata.create_all()`  |
| Any other URL (prod, staging, on-disk dev) | `alembic upgrade head` at app startup |
| Manual / CI                      | `alembic upgrade head` (CLI)       |

`mcp_server/database.bootstrap_schema` makes the choice. In-memory tests
can't share a `StaticPool` connection with the separate engine Alembic
would open, so we keep `create_all` for them.

## Layout

```
alembic.ini                     — Alembic CLI config
migrations/
├── env.py                      — entry point, reads MCP_DATABASE_URL
├── script.py.mako              — revision template
└── versions/
    └── 0001_initial_schema.py  — initial schema
```

The URL is **not** hard-coded in `alembic.ini` — `env.py` reads it from:

1. `-x dburl=...` on the alembic CLI (highest priority)
2. `MCP_DATABASE_URL` environment variable
3. `alembic.ini`'s placeholder (unreachable — fails loudly)

This keeps the migration tool using the same config source as the running
app.

## Creating a new migration

```bash
# Make the model change in mcp_server/models.py, then autogenerate:
MCP_DATABASE_URL="sqlite:///./skillful_mcp.db" \
    alembic revision --autogenerate -m "add tenants table"

# Review the generated file under migrations/versions/.
# Autogen misses some things (constraint renames, CHECK constraints,
# server defaults). Hand-edit as needed.

# Apply:
MCP_DATABASE_URL="sqlite:///./skillful_mcp.db" alembic upgrade head
```

## Applying migrations

```bash
# Upgrade to head (run by the app at startup, also the CI gate):
alembic upgrade head

# Roll back one step:
alembic downgrade -1

# Show current revision:
alembic current

# History:
alembic history
```

## Writing migrations — conventions

- **Always additive first.** Prefer add-column over rename-column in a
  single deploy. Split schema/code rollouts so the code can run against
  both old and new schema during the deploy window.
- **Use `op.batch_alter_table`** for column drops / type changes on SQLite
  — `render_as_batch=True` is already set in `env.py` for SQLite.
- **Hand-check autogen output.** Autogen doesn't know about your intent:
  it sees *that* two columns diverge, not *that you meant to rename*.
  Always read the diff.
- **Write a real `downgrade()`.** CI blocks PRs that can't reverse
  locally. Disaster recovery needs it.
- **Don't touch existing revisions after merge.** Edit a shipped
  migration and you'll divergence on any DB that already applied it.
  Add a follow-up migration instead.

## Testing

`tests/test_migrations.py`:

- **`test_upgrade_head_builds_expected_schema`** — runs `alembic upgrade
  head` on a fresh SQLite file and asserts the resulting table/column set
  matches `Base.metadata`. Catches "PR changes a model but forgets to
  generate a migration" drift.
- **`test_downgrade_is_reversible`** — `downgrade base` leaves no
  application tables (only the `alembic_version` bookkeeping).
- **`TestMigrationsPostgres`** — same assertions against a real Postgres.
  Gated on `MCP_TEST_POSTGRES_URL` env var; skipped otherwise. Local
  `make test` stays single-binary.

## Running Postgres locally

```bash
# Disposable Postgres for dev + tests
docker run -d --rm --name mcp-postgres \
  -e POSTGRES_PASSWORD=postgres \
  -p 5432:5432 postgres:16

export MCP_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres
pip install -e ".[postgres]"

# Run the app
make serve

# Or run the Postgres-gated tests:
MCP_TEST_POSTGRES_URL=$MCP_DATABASE_URL pytest tests/test_migrations.py -v
```

## Future work

- **Advisory locking at startup** — when multiple replicas boot
  simultaneously, they race to `alembic upgrade head`. Postgres advisory
  locks prevent duplicate migration runs. File locks for SQLite.
- **CI gate**: block PRs that modify `mcp_server/models.py` without a
  matching new revision file. A `pre-commit` check running
  `alembic check` (new in 1.13) is the simplest path.
- **Pre-generate SQL for DBA review** — `alembic upgrade head --sql`
  in CI for prod deploys.
- **Automated rollback** on failed blue/green deploys (productization
  §3.7 P1).
