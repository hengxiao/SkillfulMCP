"""
Wave 4 tests: key-ring rotation, revocation list, bounded expires_in,
`jti` on every token, `POST /admin/tokens/revoke`.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient
from jose import jwt

from mcp_server.auth import (
    TokenService,
    get_default_service,
    reset_default_service,
)
from mcp_server.config import get_settings
from mcp_server.keyring import KeyRing, build_keyring
from mcp_server.main import create_app
from mcp_server.revocation import RevocationList

from tests.conftest import (
    ADMIN_HEADERS,
    bearer,
    get_token,
    make_agent,
    make_skill,
)


# ---------------------------------------------------------------------------
# KeyRing
# ---------------------------------------------------------------------------

class TestKeyRing:
    def test_legacy_single_secret_mode(self, monkeypatch):
        monkeypatch.delenv("MCP_JWT_KEYS", raising=False)
        monkeypatch.setenv("MCP_JWT_SECRET", "legacy-secret")
        get_settings.cache_clear()
        try:
            kr = build_keyring(get_settings())
            assert kr.known_kids == ["primary"]
            assert kr.active_kid == "primary"
            assert kr.active_secret == "legacy-secret"
        finally:
            get_settings.cache_clear()

    def test_multi_key_mode(self, monkeypatch):
        monkeypatch.setenv(
            "MCP_JWT_KEYS", json.dumps({"k1": "s1", "k2": "s2"})
        )
        monkeypatch.setenv("MCP_JWT_ACTIVE_KID", "k2")
        monkeypatch.setenv("MCP_JWT_SECRET", "ignored-in-this-mode")
        get_settings.cache_clear()
        try:
            kr = build_keyring(get_settings())
            assert set(kr.known_kids) == {"k1", "k2"}
            assert kr.active_kid == "k2"
            assert kr.active_secret == "s2"
            assert kr.get_secret("k1") == "s1"
            assert kr.get_secret("unknown") is None
        finally:
            get_settings.cache_clear()

    def test_multi_key_missing_active_kid_rejected(self, monkeypatch):
        monkeypatch.setenv("MCP_JWT_KEYS", json.dumps({"k1": "s1"}))
        monkeypatch.setenv("MCP_JWT_ACTIVE_KID", "k-nonexistent")
        get_settings.cache_clear()
        try:
            with pytest.raises(RuntimeError, match="MCP_JWT_ACTIVE_KID"):
                build_keyring(get_settings())
        finally:
            get_settings.cache_clear()

    def test_multi_key_bad_json_rejected(self, monkeypatch):
        monkeypatch.setenv("MCP_JWT_KEYS", "not-json")
        get_settings.cache_clear()
        try:
            with pytest.raises(RuntimeError, match="not valid JSON"):
                build_keyring(get_settings())
        finally:
            get_settings.cache_clear()


# ---------------------------------------------------------------------------
# RevocationList
# ---------------------------------------------------------------------------

class TestRevocationList:
    def test_revoke_and_check(self):
        rl = RevocationList()
        assert rl.is_revoked("abc") is False
        rl.revoke("abc")
        assert rl.is_revoked("abc") is True

    def test_expired_entry_is_auto_purged(self):
        rl = RevocationList()
        rl.revoke("abc", expires_at=time.time() - 10)  # already past
        assert rl.is_revoked("abc") is False
        assert len(rl) == 0  # lazy purge happened

    def test_purge_expired(self):
        rl = RevocationList()
        rl.revoke("a", expires_at=time.time() - 10)
        rl.revoke("b", expires_at=time.time() + 600)
        removed = rl.purge_expired()
        assert removed == 1
        assert rl.is_revoked("b") is True

    def test_empty_jti_is_never_revoked(self):
        rl = RevocationList()
        rl.revoke("")
        assert rl.is_revoked("") is False

    def test_reset_clears_all(self):
        rl = RevocationList()
        rl.revoke("x")
        rl.reset()
        assert rl.is_revoked("x") is False


# ---------------------------------------------------------------------------
# TokenService — issuing / validating
# ---------------------------------------------------------------------------

class TestTokenServiceIssuance:
    def test_every_token_has_a_jti(self, client):
        make_agent(client, id="agent-a")
        t1 = get_token(client, "agent-a")
        t2 = get_token(client, "agent-a")
        c1 = jwt.get_unverified_claims(t1)
        c2 = jwt.get_unverified_claims(t2)
        assert c1["jti"] and c2["jti"]
        assert c1["jti"] != c2["jti"]

    def test_kid_header_present(self, client):
        make_agent(client, id="agent-a")
        token = get_token(client, "agent-a")
        header = jwt.get_unverified_header(token)
        assert header.get("kid") == "primary"

    def test_expires_in_clamped_to_max(self, client, monkeypatch):
        # Set a 60-second cap and ask for a full day.
        monkeypatch.setenv("MCP_MAX_TOKEN_LIFETIME_SECONDS", "60")
        get_settings.cache_clear()
        reset_default_service()
        make_agent(client, id="agent-a")
        r = client.post(
            "/token",
            json={"agent_id": "agent-a", "expires_in": 86400},
            headers=ADMIN_HEADERS,
        )
        r.raise_for_status()
        token = r.json()["access_token"]
        claims = jwt.get_unverified_claims(token)
        assert claims["exp"] - claims["iat"] == 60
        get_settings.cache_clear()

    def test_negative_expires_in_clamped_to_one(self, client):
        make_agent(client, id="agent-a")
        r = client.post(
            "/token",
            json={"agent_id": "agent-a", "expires_in": -999},
            headers=ADMIN_HEADERS,
        )
        r.raise_for_status()
        token = r.json()["access_token"]
        claims = jwt.get_unverified_claims(token)
        assert claims["exp"] - claims["iat"] == 1  # min


# ---------------------------------------------------------------------------
# Validation: kid routing, revocation
# ---------------------------------------------------------------------------

class TestTokenServiceValidation:
    def test_unknown_kid_rejected(self, client):
        make_agent(client, id="agent-a", skills=["s1"])
        # Hand-build a token with an unknown kid.
        settings = get_settings()
        bogus = jwt.encode(
            {
                "sub": "agent-a", "iss": settings.jwt_issuer,
                "iat": 0, "exp": int(time.time()) + 600,
                "jti": "x", "skillsets": [], "skills": ["s1"], "scope": ["read"],
            },
            settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
            headers={"kid": "no-such-kid"},
        )
        r = client.get("/skills", headers=bearer(bogus))
        assert r.status_code == 401
        assert "Unknown signing key" in r.json()["detail"]

    def test_old_kid_still_verifies_during_rotation(self, monkeypatch):
        """A token signed with key `old` verifies after rotation to `new`
        as long as `old` is still in MCP_JWT_KEYS."""
        monkeypatch.setenv(
            "MCP_JWT_KEYS", json.dumps({"old": "secret-old", "new": "secret-new"})
        )
        monkeypatch.setenv("MCP_JWT_ACTIVE_KID", "new")
        get_settings.cache_clear()
        reset_default_service()

        app = create_app(database_url="sqlite:///:memory:")
        with TestClient(app) as c:
            make_agent(c, id="agent-a", skills=["s1"])
            make_skill(c, id="s1")
            # Hand-mint a token signed with the OLD key.
            settings = get_settings()
            old_token = jwt.encode(
                {
                    "sub": "agent-a", "iss": settings.jwt_issuer,
                    "iat": int(time.time()),
                    "exp": int(time.time()) + 600,
                    "jti": "old-sig-jti",
                    "skillsets": [], "skills": ["s1"], "scope": ["read"],
                },
                "secret-old",
                algorithm=settings.jwt_algorithm,
                headers={"kid": "old"},
            )
            r = c.get("/skills", headers=bearer(old_token))
            assert r.status_code == 200

        get_settings.cache_clear()

    def test_revoked_jti_rejected(self, client):
        make_agent(client, id="agent-a", skills=["s1"])
        make_skill(client, id="s1")
        token = get_token(client, "agent-a")
        jti = jwt.get_unverified_claims(token)["jti"]

        # Prove the token works first.
        assert client.get("/skills", headers=bearer(token)).status_code == 200

        # Revoke it.
        get_default_service().revocation.revoke(jti)

        r = client.get("/skills", headers=bearer(token))
        assert r.status_code == 401
        assert "revoked" in r.json()["detail"].lower()

    def test_revoking_unknown_jti_is_noop(self, client):
        # Revoking a jti that was never issued must not break anything.
        get_default_service().revocation.revoke("never-seen")
        make_agent(client, id="agent-a", skills=["s1"])
        make_skill(client, id="s1")
        token = get_token(client, "agent-a")
        r = client.get("/skills", headers=bearer(token))
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Admin revoke endpoint
# ---------------------------------------------------------------------------

class TestAdminRevokeEndpoint:
    def test_revoke_via_admin_endpoint(self, client):
        make_agent(client, id="agent-a", skills=["s1"])
        make_skill(client, id="s1")
        token = get_token(client, "agent-a")
        jti = jwt.get_unverified_claims(token)["jti"]

        r = client.post(
            "/admin/tokens/revoke",
            json={"jti": jti},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 204

        # Now the token should be rejected.
        r2 = client.get("/skills", headers=bearer(token))
        assert r2.status_code == 401

    def test_revoke_requires_admin(self, client):
        r = client.post("/admin/tokens/revoke", json={"jti": "x"})
        assert r.status_code == 403

    def test_revoke_empty_jti_rejected(self, client):
        r = client.post(
            "/admin/tokens/revoke",
            json={"jti": ""},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 400

    def test_revoked_count_endpoint(self, client):
        r0 = client.get("/admin/tokens/revoked-count", headers=ADMIN_HEADERS)
        assert r0.status_code == 200
        n0 = r0.json()["count"]

        client.post(
            "/admin/tokens/revoke",
            json={"jti": "abc"},
            headers=ADMIN_HEADERS,
        )
        r1 = client.get("/admin/tokens/revoked-count", headers=ADMIN_HEADERS)
        assert r1.json()["count"] == n0 + 1
