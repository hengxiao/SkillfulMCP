"""
Tests that keep Alembic migrations and `Base.metadata` in lockstep.

These are regression catchers for the classic "PR adds a model change but
forgets the migration" failure mode. They do NOT replace running Postgres
in CI — migration idempotency + dialect quirks still need a real Postgres
smoke test. That's deferred to Wave 2's follow-up when the CI pipeline
lands.

Anything in this module that needs Postgres is gated on the
`MCP_TEST_POSTGRES_URL` env var so local `make test` stays single-binary.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from mcp_server.models import Base


ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def _apply_migrations(url: str) -> None:
    cfg = Config(str(ALEMBIC_INI))
    old = os.environ.get("MCP_DATABASE_URL")
    os.environ["MCP_DATABASE_URL"] = url
    try:
        command.upgrade(cfg, "head")
    finally:
        if old is None:
            os.environ.pop("MCP_DATABASE_URL", None)
        else:
            os.environ["MCP_DATABASE_URL"] = old


def _tables_and_columns(engine) -> dict[str, set[str]]:
    inspector = inspect(engine)
    return {
        t: {c["name"] for c in inspector.get_columns(t)}
        for t in inspector.get_table_names()
        if t != "alembic_version"
    }


def _expected_from_metadata() -> dict[str, set[str]]:
    return {
        t.name: {c.name for c in t.columns}
        for t in Base.metadata.sorted_tables
    }


# ---------------------------------------------------------------------------
# SQLite — always run
# ---------------------------------------------------------------------------

class TestMigrationsSQLite:
    def test_env_py_does_not_disable_existing_loggers(self):
        """Regression: `migrations/env.py` previously called
        `fileConfig(alembic.ini)` which by default set
        `disable_existing_loggers=True`. Inside the catalog app's
        lifespan, that silently turned off uvicorn's `uvicorn.error`
        logger — the one uvicorn uses to report lifespan completion back
        to its startup poller. Result: the worker successfully ran
        migrations then exited with code 1 right after. Found by a live
        smoke test in Phase A of the test-hardening pass; this test
        ensures we don't accidentally re-break it."""
        import logging
        # Create some real loggers to observe state.
        before = logging.getLogger("uvicorn.error")
        before.addHandler(logging.NullHandler())
        assert before.disabled is False  # sanity: baseline state

        # Load the migration env module in the same way the catalog's
        # lifespan does. We don't actually run migrations — we just
        # exercise the fileConfig call at import time.
        from alembic.config import Config

        cfg = Config(str(ALEMBIC_INI))
        # Creating a ScriptDirectory forces alembic.ini to be parsed but
        # does NOT run env.py. We need env.py to run, which happens on
        # any alembic command. Simulate by invoking command.history
        # which is cheap and loads env.py.
        from alembic import command
        import os
        # Point at an existing (post-migration) DB that our tests create,
        # or just a memory URL — fileConfig runs regardless.
        old = os.environ.get("MCP_DATABASE_URL")
        os.environ["MCP_DATABASE_URL"] = "sqlite:///:memory:"
        try:
            command.history(cfg)
        finally:
            if old is None:
                os.environ.pop("MCP_DATABASE_URL", None)
            else:
                os.environ["MCP_DATABASE_URL"] = old

        after = logging.getLogger("uvicorn.error")
        # The key assertion: the logger is not in the "disabled" state.
        assert after.disabled is False, (
            "migrations/env.py disabled an existing logger — lifespan "
            "will exit uvicorn with code 1 after migrations run. "
            "Check the fileConfig call has disable_existing_loggers=False."
        )

    def test_env_py_reinstalls_json_formatter_after_fileconfig(self):
        """Regression for the Loki smoke test that surfaced this bug:
        alembic.ini declares a `console` handler on the root logger
        with a plain text formatter (``INFO  [name] msg``). When the
        catalog app's lifespan runs migrations, the fileConfig call
        replaces the JSONFormatter our `configure_logging()` just
        installed with the alembic text formatter, so every log line
        from the running worker emits as text instead of JSON from
        that point on.

        The fix in migrations/env.py re-calls
        `configure_logging(force=True)` right after fileConfig. Pin
        that here so a future edit can't silently re-clobber the
        formatter."""
        import logging

        from mcp_server.logging_config import JSONFormatter, configure_logging

        configure_logging(force=True)
        # Ensure the JSON formatter is the active root formatter BEFORE.
        assert any(
            isinstance(h.formatter, JSONFormatter)
            for h in logging.root.handlers
        )

        from alembic import command
        from alembic.config import Config

        cfg = Config(str(ALEMBIC_INI))
        import os

        old = os.environ.get("MCP_DATABASE_URL")
        os.environ["MCP_DATABASE_URL"] = "sqlite:///:memory:"
        try:
            command.history(cfg)
        finally:
            if old is None:
                os.environ.pop("MCP_DATABASE_URL", None)
            else:
                os.environ["MCP_DATABASE_URL"] = old

        # The JSON formatter must still be on root. A regression in
        # env.py (e.g. removing the force=True re-install) would
        # swap in the alembic.ini `generic` formatter and this
        # assertion would fail.
        assert any(
            isinstance(h.formatter, JSONFormatter)
            for h in logging.root.handlers
        ), (
            "migrations/env.py's fileConfig() replaced root handlers "
            "without re-installing the JSON formatter. Container log "
            "output will no longer be structured JSON."
        )

    def test_upgrade_head_builds_expected_schema(self, tmp_path):
        """After `alembic upgrade head`, every model table exists with
        the columns declared in `Base.metadata`."""
        db_path = tmp_path / "mig.db"
        url = f"sqlite:///{db_path}"

        _apply_migrations(url)

        engine = create_engine(url)
        have = _tables_and_columns(engine)
        expect = _expected_from_metadata()

        # Same table set.
        assert set(have.keys()) == set(expect.keys()), (
            f"tables diverge. migration built {sorted(have)!r}; "
            f"models expect {sorted(expect)!r}"
        )
        # Same columns per table.
        for table, cols in expect.items():
            assert have[table] == cols, (
                f"columns for {table!r} diverge. migration: {sorted(have[table])!r}; "
                f"models: {sorted(cols)!r}"
            )

    def test_downgrade_is_reversible(self, tmp_path):
        """`downgrade base` wipes everything the initial migration created."""
        db_path = tmp_path / "mig.db"
        url = f"sqlite:///{db_path}"
        _apply_migrations(url)

        cfg = Config(str(ALEMBIC_INI))
        old = os.environ.get("MCP_DATABASE_URL")
        os.environ["MCP_DATABASE_URL"] = url
        try:
            command.downgrade(cfg, "base")
        finally:
            if old is None:
                os.environ.pop("MCP_DATABASE_URL", None)
            else:
                os.environ["MCP_DATABASE_URL"] = old

        engine = create_engine(url)
        tables = set(inspect(engine).get_table_names()) - {"alembic_version"}
        assert tables == set(), f"leftover tables after downgrade: {tables!r}"


# ---------------------------------------------------------------------------
# Postgres — only if MCP_TEST_POSTGRES_URL is set
# ---------------------------------------------------------------------------

_PG_URL = os.environ.get("MCP_TEST_POSTGRES_URL")
_NEEDS_PG = pytest.mark.skipif(
    not _PG_URL, reason="set MCP_TEST_POSTGRES_URL to run Postgres tests"
)


@_NEEDS_PG
class TestMigrationsPostgres:
    def test_upgrade_head_against_postgres(self):
        """Same parity check, against a real Postgres. Requires the env var
        to point at a disposable DB (tests drop + recreate tables)."""
        engine = create_engine(_PG_URL)
        # Start from empty — drop any leftover tables from prior runs.
        Base.metadata.drop_all(engine)
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP TABLE IF EXISTS alembic_version CASCADE")

        _apply_migrations(_PG_URL)

        have = _tables_and_columns(engine)
        expect = _expected_from_metadata()
        assert set(have.keys()) == set(expect.keys())
        for table, cols in expect.items():
            assert have[table] == cols
