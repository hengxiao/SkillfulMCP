from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError

from .config import get_settings
from .database import init_db
from .errors import (
    http_exception_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)
from .logging_config import configure_logging, get_logger
from .middleware import RequestIDMiddleware
from .routers import admin, agents, bundles, health, skillsets, skills, token


def create_app(database_url: str | None = None) -> FastAPI:
    """
    Factory that creates the FastAPI application.

    Parameters
    ----------
    database_url:
        Override the database URL (useful in tests to pass sqlite:///:memory:).
        Falls back to MCP_DATABASE_URL env var, then defaults to a local SQLite file.
    """
    # Ensure structured logging is in place before we log anything from the
    # factory / lifespan. Idempotent, safe to call from every create_app.
    configure_logging()
    log = get_logger("mcp.main")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings = get_settings()
        url = database_url or settings.database_url
        app.state.session_factory = init_db(url)
        log.info("startup", extra={"database_url": _redact_url(url)})
        yield
        log.info("shutdown")

    app = FastAPI(
        title="SkillfulMCP",
        description="JWT-based skill authorization server",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Middleware (innermost last; Starlette applies them in reverse order).
    app.add_middleware(RequestIDMiddleware)

    # Global exception handlers so every error carries a consistent envelope
    # and an X-Request-ID header.
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)

    app.include_router(health.router)
    app.include_router(token.router)
    app.include_router(skills.router)
    app.include_router(bundles.router)
    app.include_router(agents.router)
    app.include_router(skillsets.router)
    app.include_router(admin.router)

    return app


def _redact_url(url: str) -> str:
    """Strip password from a connection URL before logging."""
    # sqlite:///./foo.db → unchanged. Hides passwords in postgres://user:pwd@host/db.
    if "@" in url and "://" in url:
        scheme, rest = url.split("://", 1)
        if "@" in rest:
            creds, host = rest.split("@", 1)
            if ":" in creds:
                user = creds.split(":", 1)[0]
                return f"{scheme}://{user}:***@{host}"
    return url


def run() -> None:
    uvicorn.run(
        "mcp_server.main:create_app",
        factory=True,
        host="0.0.0.0",
        port=8000,
        reload=True,
    )
