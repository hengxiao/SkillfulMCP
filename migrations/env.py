"""
Alembic environment script.

Reads the target database URL from (in priority order):
  1. The `-x dburl=...` option on the alembic CLI.
  2. The MCP_DATABASE_URL environment variable (same as the app reads).
  3. The URL declared in alembic.ini's [alembic] section.

Target metadata is `mcp_server.models.Base.metadata`, so
`alembic revision --autogenerate` picks up model changes.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from mcp_server.models import Base


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _resolve_url() -> str:
    x_args = context.get_x_argument(as_dictionary=True)
    if x_args.get("dburl"):
        return x_args["dburl"]
    env_url = os.environ.get("MCP_DATABASE_URL")
    if env_url:
        return env_url
    return config.get_main_option("sqlalchemy.url")


target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout rather than executing it — for review pipelines."""
    url = _resolve_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite needs batch operations for many ALTER TABLE scenarios.
        render_as_batch=url.startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = _resolve_url()
    cfg = config.get_section(config.config_ini_section, {})
    cfg["sqlalchemy.url"] = url
    connectable = engine_from_config(cfg, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=url.startswith("sqlite"),
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
