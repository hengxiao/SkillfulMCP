"""
Wave 3 tests: token bucket, RateLimitMiddleware, RequestSizeLimitMiddleware,
and the `?limit=` param on GET /skills.

Non-rate-limit integration tests in the rest of the suite run against the
default `client` fixture which disables the limiter via
MCP_RATE_LIMIT_PER_MINUTE=0 (set in conftest.py). The fine-grained tests
below build their own apps with explicit limits.
"""

from __future__ import annotations

import os
import time

import pytest
from fastapi.testclient import TestClient

from mcp_server.main import create_app
from mcp_server.middleware import HEADER as REQUEST_ID_HEADER
from mcp_server.ratelimit import TokenBucket

from tests.conftest import ADMIN_HEADERS, bearer, get_token, make_agent, make_skill


# ---------------------------------------------------------------------------
# TokenBucket unit tests — deterministic via `now`
# ---------------------------------------------------------------------------

class TestTokenBucketUnit:
    def test_allows_up_to_capacity(self):
        bucket = TokenBucket(rate_per_minute=60, capacity=3)
        allowed_counts = sum(bucket.allow("k", now=0.0)[0] for _ in range(3))
        assert allowed_counts == 3
        # 4th call in the same instant should be denied.
        allowed, retry_after = bucket.allow("k", now=0.0)
        assert allowed is False
        assert retry_after > 0

    def test_rate_zero_disables_the_limiter(self):
        bucket = TokenBucket(rate_per_minute=0)
        assert bucket.enabled is False
        for i in range(100):
            allowed, _ = bucket.allow("k", now=float(i))
            assert allowed is True

    def test_tokens_refill_over_time(self):
        # 60 req/min = 1 token per second; capacity 1.
        bucket = TokenBucket(rate_per_minute=60, capacity=1)
        assert bucket.allow("k", now=0.0)[0] is True
        assert bucket.allow("k", now=0.5)[0] is False  # refilled 0.5 tokens
        allowed, _ = bucket.allow("k", now=1.01)
        assert allowed is True  # refilled past a full token

    def test_per_key_isolation(self):
        bucket = TokenBucket(rate_per_minute=60, capacity=1)
        assert bucket.allow("a", now=0.0)[0] is True
        assert bucket.allow("b", now=0.0)[0] is True
        assert bucket.allow("a", now=0.0)[0] is False
        assert bucket.allow("b", now=0.0)[0] is False

    def test_retry_after_is_within_a_refill_period(self):
        # 60/min → 1/sec. If the bucket is empty, retry_after ≤ 1s.
        bucket = TokenBucket(rate_per_minute=60, capacity=1)
        bucket.allow("k", now=0.0)  # empty
        _, retry = bucket.allow("k", now=0.0)
        assert 0 < retry <= 1.0

    def test_reset_clears_state(self):
        bucket = TokenBucket(rate_per_minute=60, capacity=1)
        bucket.allow("k", now=0.0)
        assert bucket.allow("k", now=0.0)[0] is False
        bucket.reset("k")
        assert bucket.allow("k", now=0.0)[0] is True


# ---------------------------------------------------------------------------
# Middleware integration — spin up an app with custom limits
# ---------------------------------------------------------------------------

@pytest.fixture()
def limited_client(monkeypatch):
    """TestClient with rate_limit=3/min."""
    monkeypatch.setenv("MCP_RATE_LIMIT_PER_MINUTE", "3")
    # get_settings is lru_cached; force a fresh Settings by re-importing.
    from mcp_server.config import get_settings
    get_settings.cache_clear()
    app = create_app(database_url="sqlite:///:memory:")
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


class TestRateLimitMiddleware:
    def test_returns_429_after_bucket_exhausted(self, limited_client):
        # First 3 requests succeed; 4th is throttled.
        for _ in range(3):
            r = limited_client.get("/livez")
            # /livez is exempt, hits always succeed — so use a non-exempt path.
        for _ in range(3):
            r = limited_client.get(
                "/admin/skills", headers=ADMIN_HEADERS
            )
            assert r.status_code == 200
        r = limited_client.get("/admin/skills", headers=ADMIN_HEADERS)
        assert r.status_code == 429
        body = r.json()
        assert body["code"] == "RATE_LIMIT_EXCEEDED"
        assert "Retry-After" in r.headers
        assert float(r.headers["Retry-After"]) > 0

    def test_health_paths_are_exempt(self, limited_client):
        # Many more requests than the limit on /livez — should all succeed.
        for _ in range(50):
            assert limited_client.get("/livez").status_code == 200
        for _ in range(50):
            assert limited_client.get("/readyz").status_code == 200

    def test_request_id_present_on_429(self, limited_client):
        for _ in range(3):
            limited_client.get("/admin/skills", headers=ADMIN_HEADERS)
        r = limited_client.get("/admin/skills", headers=ADMIN_HEADERS)
        assert r.status_code == 429
        assert REQUEST_ID_HEADER in r.headers
        assert r.json()["request_id"] == r.headers[REQUEST_ID_HEADER]


# ---------------------------------------------------------------------------
# Request body size cap
# ---------------------------------------------------------------------------

@pytest.fixture()
def small_body_client(monkeypatch):
    """Cap request body at ~1 KB (enough for most JSON but not a bundle)."""
    monkeypatch.setenv("MCP_MAX_REQUEST_BODY_MB", "1")
    from mcp_server.config import get_settings
    get_settings.cache_clear()
    app = create_app(database_url="sqlite:///:memory:")
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


class TestRequestSizeLimit:
    def test_oversize_returns_413(self, small_body_client):
        # Upload a 2 MB archive against the 1 MB body cap.
        payload = b"\x00" * (2 * 1024 * 1024)
        # Pre-create the skill so the bundle endpoint doesn't 404 before
        # the middleware even sees the body. (It won't — middleware runs
        # first — but keeps the assertion honest.)
        make_skill(small_body_client, id="skill-a", version="1.0.0")
        r = small_body_client.post(
            "/skills/skill-a/versions/1.0.0/bundle",
            files={"file": ("big.zip", payload, "application/zip")},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 413
        body = r.json()
        assert body["code"] == "REQUEST_TOO_LARGE"

    def test_within_limit_passes_through(self, small_body_client):
        # Payload small enough to pass the size middleware, but the
        # bundle endpoint will reject it as an invalid archive (400).
        # That proves the size cap wasn't in the way.
        make_skill(small_body_client, id="skill-a", version="1.0.0")
        r = small_body_client.post(
            "/skills/skill-a/versions/1.0.0/bundle",
            files={"file": ("small.zip", b"not an archive", "application/zip")},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 400  # BundleError, not 413


# ---------------------------------------------------------------------------
# GET /skills?limit=
# ---------------------------------------------------------------------------

class TestSkillsLimit:
    def _auth(self, client, *, skill_ids):
        make_agent(client, id="api-agent", skills=skill_ids, scope=["read"])
        return bearer(get_token(client, "api-agent"))

    def test_limit_caps_results(self, client):
        for i in range(5):
            make_skill(client, id=f"skill-{i}")
        headers = self._auth(client, skill_ids=[f"skill-{i}" for i in range(5)])
        r = client.get("/skills?limit=2", headers=headers)
        assert r.status_code == 200
        assert len(r.json()) == 2

    def test_no_limit_returns_all_authorized(self, client):
        for i in range(3):
            make_skill(client, id=f"skill-{i}")
        headers = self._auth(client, skill_ids=[f"skill-{i}" for i in range(3)])
        r = client.get("/skills", headers=headers)
        assert r.status_code == 200
        assert len(r.json()) == 3

    def test_limit_zero_is_rejected(self, client):
        headers = self._auth(client, skill_ids=["x"])
        r = client.get("/skills?limit=0", headers=headers)
        assert r.status_code == 422  # pydantic ge=1

    def test_limit_over_ceiling_is_rejected(self, client):
        headers = self._auth(client, skill_ids=["x"])
        r = client.get("/skills?limit=999999", headers=headers)
        assert r.status_code == 422  # pydantic le=10_000
