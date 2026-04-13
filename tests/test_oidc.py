"""OIDC login tests (item G).

Service unit tests cover config parsing + URL building + token
exchange + verification. Route tests use monkeypatching to stub
the provider round-trips; a full mock OIDC server is out of scope
— we pin the happy path + the obvious failure modes.
"""

from __future__ import annotations

import time

import httpx
import pytest
from fastapi.testclient import TestClient
from jose import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

from webui import oidc as oidc_mod
from webui.main import create_app


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class TestOIDCConfig:
    def test_none_without_required_values(self, monkeypatch):
        for k in ("MCP_OIDC_ISSUER_URL", "MCP_OIDC_CLIENT_ID",
                  "MCP_OIDC_CLIENT_SECRET", "MCP_OIDC_REDIRECT_URI"):
            monkeypatch.delenv(k, raising=False)
        assert oidc_mod.OIDCConfig.from_env() is None
        assert oidc_mod.is_enabled() is False

    def test_loads_when_all_set(self, monkeypatch):
        monkeypatch.setenv("MCP_OIDC_ISSUER_URL", "https://example.auth0.com/")
        monkeypatch.setenv("MCP_OIDC_CLIENT_ID", "abc")
        monkeypatch.setenv("MCP_OIDC_CLIENT_SECRET", "secret")
        monkeypatch.setenv("MCP_OIDC_REDIRECT_URI", "https://mcp.test/auth/oidc/callback")
        cfg = oidc_mod.OIDCConfig.from_env()
        assert cfg is not None
        assert cfg.issuer_url == "https://example.auth0.com"  # trailing / stripped
        assert cfg.scopes == "openid email profile"


# ---------------------------------------------------------------------------
# Login URL + token exchange
# ---------------------------------------------------------------------------

def _make_rsa_keypair():
    """Generate a keypair + JWK dict for a fake provider."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    # JWK representation of the public key.
    import base64
    numbers = private.public_key().public_numbers()

    def b64u(n):
        return base64.urlsafe_b64encode(
            n.to_bytes((n.bit_length() + 7) // 8, "big")
        ).rstrip(b"=").decode()
    jwk = {
        "kty": "RSA",
        "kid": "test-kid",
        "use": "sig",
        "alg": "RS256",
        "n": b64u(numbers.n),
        "e": b64u(numbers.e),
    }
    return pem, jwk


@pytest.fixture()
def oidc_env(monkeypatch):
    monkeypatch.setenv("MCP_OIDC_ISSUER_URL", "https://example.auth0.com")
    monkeypatch.setenv("MCP_OIDC_CLIENT_ID", "abc")
    monkeypatch.setenv("MCP_OIDC_CLIENT_SECRET", "secret")
    monkeypatch.setenv(
        "MCP_OIDC_REDIRECT_URI", "https://mcp.test/auth/oidc/callback"
    )
    oidc_mod.clear_caches()
    yield oidc_mod.OIDCConfig.from_env()
    oidc_mod.clear_caches()


def _transport_for(issuer: str, *, token_body=None, jwks_body=None,
                   token_status: int = 200):
    """Return an httpx.MockTransport that serves discovery, jwks,
    token endpoints for one issuer."""
    disc = {
        "issuer": issuer,
        "authorization_endpoint": issuer + "/authorize",
        "token_endpoint": issuer + "/oauth/token",
        "jwks_uri": issuer + "/.well-known/jwks.json",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(200, json=disc)
        if request.url.path == "/.well-known/jwks.json":
            return httpx.Response(200, json=jwks_body)
        if request.url.path == "/oauth/token":
            return httpx.Response(token_status, json=token_body or {})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


class TestLoginURL:
    def test_builds_authorize_url(self, oidc_env, monkeypatch):
        # Pre-warm the discovery cache so build_login_url doesn't
        # need a live HTTP client.
        oidc_mod._discovery_cache[oidc_env.issuer_url] = {
            "authorization_endpoint": oidc_env.issuer_url + "/authorize",
            "token_endpoint": oidc_env.issuer_url + "/oauth/token",
            "jwks_uri": oidc_env.issuer_url + "/.well-known/jwks.json",
            "issuer": oidc_env.issuer_url,
        }
        url = oidc_mod.build_login_url(
            oidc_env, state="st", nonce="nn",
        )
        assert url.startswith(oidc_env.issuer_url + "/authorize?")
        assert "client_id=abc" in url
        assert "state=st" in url
        assert "nonce=nn" in url
        assert "response_type=code" in url


class TestExchangeAndVerify:
    def _make_claims(self, *, iss, aud, nonce, email="alice@example.com"):
        return {
            "iss": iss, "aud": aud, "sub": "user-123",
            "email": email, "nonce": nonce,
            "iat": int(time.time()), "exp": int(time.time()) + 300,
        }

    def test_happy_path(self, oidc_env, monkeypatch):
        pem, jwk = _make_rsa_keypair()
        claims = self._make_claims(
            iss=oidc_env.issuer_url, aud=oidc_env.client_id, nonce="abc"
        )
        id_token = jwt.encode(
            claims, pem, algorithm="RS256", headers={"kid": jwk["kid"]}
        )
        transport = _transport_for(
            oidc_env.issuer_url,
            token_body={"id_token": id_token},
            jwks_body={"keys": [jwk]},
        )
        http = httpx.Client(transport=transport)
        try:
            result = oidc_mod.exchange_and_verify(
                oidc_env, code="xyz", nonce="abc", http_client=http,
            )
        finally:
            http.close()
        assert result["email"] == "alice@example.com"
        assert result["sub"] == "user-123"

    def test_bad_nonce(self, oidc_env, monkeypatch):
        pem, jwk = _make_rsa_keypair()
        claims = self._make_claims(
            iss=oidc_env.issuer_url, aud=oidc_env.client_id,
            nonce="from-provider",
        )
        id_token = jwt.encode(
            claims, pem, algorithm="RS256", headers={"kid": jwk["kid"]}
        )
        transport = _transport_for(
            oidc_env.issuer_url,
            token_body={"id_token": id_token},
            jwks_body={"keys": [jwk]},
        )
        http = httpx.Client(transport=transport)
        with pytest.raises(oidc_mod.OIDCError, match="nonce mismatch"):
            try:
                oidc_mod.exchange_and_verify(
                    oidc_env, code="xyz", nonce="expected-different",
                    http_client=http,
                )
            finally:
                http.close()

    def test_token_endpoint_error(self, oidc_env):
        transport = _transport_for(
            oidc_env.issuer_url,
            token_status=400, token_body={"error": "bad_code"},
        )
        http = httpx.Client(transport=transport)
        with pytest.raises(oidc_mod.OIDCError, match="token exchange failed"):
            try:
                oidc_mod.exchange_and_verify(
                    oidc_env, code="xyz", nonce="n", http_client=http,
                )
            finally:
                http.close()


# ---------------------------------------------------------------------------
# Login page OIDC button
# ---------------------------------------------------------------------------

class TestLoginPageIntegration:
    def test_button_hidden_when_not_configured(self, monkeypatch):
        for k in ("MCP_OIDC_ISSUER_URL", "MCP_OIDC_CLIENT_ID",
                  "MCP_OIDC_CLIENT_SECRET", "MCP_OIDC_REDIRECT_URI"):
            monkeypatch.delenv(k, raising=False)
        app = create_app()
        with TestClient(app) as c:
            r = c.get("/login")
            assert r.status_code == 200
            assert b"Sign in with SSO" not in r.content

    def test_button_shown_when_configured(self, oidc_env):
        app = create_app()
        with TestClient(app) as c:
            r = c.get("/login")
            assert r.status_code == 200
            assert b"Sign in with SSO" in r.content
