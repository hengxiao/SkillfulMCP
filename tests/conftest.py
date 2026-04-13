"""
Shared fixtures for the SkillfulMCP test suite.

Environment variables are set before any mcp_server import so that
get_settings() (which is lru_cache'd) picks them up on first call.
"""

import os

# Must be set before importing mcp_server
os.environ.setdefault("MCP_JWT_SECRET", "test-secret-key-for-testing-only")
os.environ.setdefault("MCP_ADMIN_KEY", "test-admin-key")
os.environ.setdefault("MCP_DATABASE_URL", "sqlite:///:memory:")
# Disable the rate limiter for the default test client so unrelated
# integration tests never flake. Rate-limit tests in test_rate_limit.py
# construct their own apps with an explicit limit.
os.environ.setdefault("MCP_RATE_LIMIT_PER_MINUTE", "0")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from mcp_server.auth import reset_default_service
from mcp_server.config import get_settings
from mcp_server.database import init_db
from mcp_server.main import create_app
from mcp_server.models import Base


# ---------------------------------------------------------------------------
# Autouse: reset the auth singleton between tests
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_auth_singleton():
    """Clear the module-level TokenService between tests so revocation
    state doesn't leak across cases. Also lets a test that sets
    MCP_JWT_KEYS via monkeypatch get a fresh keyring."""
    reset_default_service()
    yield
    reset_default_service()


@pytest.fixture(autouse=True)
def _reset_bundle_store():
    """Clear the module-level default BundleStore between tests so that
    a test that switches to MCP_BUNDLE_STORE=s3 doesn't poison later
    inline-mode tests."""
    from mcp_server.bundles import reset_default_store
    reset_default_store()
    yield
    reset_default_store()


# ---------------------------------------------------------------------------
# Constants used across tests
# ---------------------------------------------------------------------------

ADMIN_KEY = "test-admin-key"
ADMIN_HEADERS = {"X-Admin-Key": ADMIN_KEY}
JWT_SECRET = "test-secret-key-for-testing-only"


# ---------------------------------------------------------------------------
# Raw SQLAlchemy session (for unit-testing service layer directly)
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_session():
    """In-memory SQLite session for direct service-layer tests."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    Base.metadata.drop_all(engine)


# ---------------------------------------------------------------------------
# FastAPI TestClient (for API integration tests)
# ---------------------------------------------------------------------------

@pytest.fixture()
def client():
    """Full app with a fresh in-memory DB for each test function."""
    app = create_app(database_url="sqlite:///:memory:")
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def admin(client):
    """Shorthand: (client, admin_headers) tuple."""
    return client, ADMIN_HEADERS


# ---------------------------------------------------------------------------
# Convenience helpers used in multiple test modules
# ---------------------------------------------------------------------------

def make_skillset(client, *, id="test-ss", name="Test Skillset"):
    r = client.post(
        "/skillsets",
        json={"id": id, "name": name, "description": ""},
        headers=ADMIN_HEADERS,
    )
    r.raise_for_status()
    return r.json()


def make_skill(client, *, id="skill-a", version="1.0.0", skillset_ids=None):
    payload = {
        "id": id,
        "name": f"Skill {id}",
        "description": "A test skill",
        "version": version,
        "metadata": {"tag": "test"},
        "skillset_ids": skillset_ids or [],
    }
    r = client.post("/skills", json=payload, headers=ADMIN_HEADERS)
    r.raise_for_status()
    return r.json()


def make_agent(client, *, id="agent-1", skillsets=None, skills=None, scope=None):
    payload = {
        "id": id,
        "name": f"Agent {id}",
        "skillsets": skillsets or [],
        "skills": skills or [],
        "scope": scope or ["read"],
    }
    r = client.post("/agents", json=payload, headers=ADMIN_HEADERS)
    r.raise_for_status()
    return r.json()


def get_token(client, agent_id: str) -> str:
    r = client.post(
        "/token",
        json={"agent_id": agent_id, "expires_in": 3600},
        headers=ADMIN_HEADERS,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}
