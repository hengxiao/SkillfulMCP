from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from .config import get_settings
from .database import init_db
from .routers import admin, agents, health, skillsets, skills, token


def create_app(database_url: str | None = None) -> FastAPI:
    """
    Factory that creates the FastAPI application.

    Parameters
    ----------
    database_url:
        Override the database URL (useful in tests to pass sqlite:///:memory:).
        Falls back to MCP_DATABASE_URL env var, then defaults to a local SQLite file.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings = get_settings()
        url = database_url or settings.database_url
        app.state.session_factory = init_db(url)
        yield

    app = FastAPI(
        title="SkillfulMCP",
        description="JWT-based skill authorization server",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.include_router(health.router)
    app.include_router(token.router)
    app.include_router(skills.router)
    app.include_router(agents.router)
    app.include_router(skillsets.router)
    app.include_router(admin.router)

    return app


def run() -> None:
    uvicorn.run(
        "mcp_server.main:create_app",
        factory=True,
        host="0.0.0.0",
        port=8000,
        reload=True,
    )
