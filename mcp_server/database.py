from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from .logging_config import get_logger
from .models import Base


log = get_logger("mcp.database")

# Path to the alembic.ini at the repo root; shared across this process.
_ALEMBIC_CFG_PATH = Path(__file__).resolve().parent.parent / "alembic.ini"


# ---------------------------------------------------------------------------
# Engine construction
# ---------------------------------------------------------------------------

def make_engine(url: str) -> Engine:
    if url == "sqlite:///:memory:":
        engine = create_engine(
            url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    else:
        kwargs: dict = {}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        engine = create_engine(url, **kwargs)

    # SQLite: enforce foreign keys so `ondelete=CASCADE` actually fires and
    # dev behavior matches Postgres. No-op on other dialects.
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def _enable_sqlite_fk(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def make_session_factory(engine) -> sessionmaker:
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------

def _run_alembic_upgrade(url: str) -> None:
    """Apply `alembic upgrade head` to `url`."""
    cfg = Config(str(_ALEMBIC_CFG_PATH))
    # Inject URL via the environment so env.py picks it up. (We could also
    # cfg.set_main_option, but the env-var path is the same one the CLI uses.)
    import os
    old = os.environ.get("MCP_DATABASE_URL")
    os.environ["MCP_DATABASE_URL"] = url
    try:
        command.upgrade(cfg, "head")
    finally:
        if old is None:
            os.environ.pop("MCP_DATABASE_URL", None)
        else:
            os.environ["MCP_DATABASE_URL"] = old


def bootstrap_schema(engine: Engine, url: str) -> None:
    """
    Ensure the schema matches `Base.metadata` for this URL.

    - For `sqlite:///:memory:` (used by the test suite) we call
      `Base.metadata.create_all` directly. Alembic migrations are overkill
      for the throwaway in-memory DBs created per test, and the StaticPool
      means a single shared connection — running `alembic upgrade head` in
      a new connection wouldn't see the same database anyway.
    - Every other URL runs `alembic upgrade head`. Production, staging, and
      the default on-disk dev SQLite file all take this path, so migrations
      stay the source of truth.
    """
    if url == "sqlite:///:memory:":
        log.info("bootstrap", extra={"mode": "create_all", "url": url})
        Base.metadata.create_all(engine)
        return
    log.info("bootstrap", extra={"mode": "alembic_upgrade_head", "url": url})
    _run_alembic_upgrade(url)


def init_db(url: str) -> sessionmaker:
    """Create engine, apply schema, return a session factory."""
    engine = make_engine(url)
    bootstrap_schema(engine, url)
    return make_session_factory(engine)
