"""Asymmetric JWT + JWKS endpoint tests (item I).

Symmetric (HS256) is the default; `/.well-known/jwks.json` returns
an empty key set. When a PEM private key is configured, the ring
switches to RS256 and JWKS publishes the derived public JWK.
"""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jose import jwt

from mcp_server.keyring import build_keyring, public_jwks
from mcp_server.main import create_app


def _rsa_private_pem() -> str:
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")


class TestKeyringSymmetric:
    def test_hmac_is_default(self, client):
        # Reach into the running app to inspect its keyring.
        from mcp_server.auth import get_default_service
        ring = get_default_service().keyring
        assert ring.algorithm == "HS256"
        assert ring.is_asymmetric is False
        assert public_jwks(ring) == {"keys": []}


class TestJWKSEndpoint:
    def test_hmac_returns_empty_key_set(self, client):
        r = client.get("/.well-known/jwks.json")
        assert r.status_code == 200
        assert r.json() == {"keys": []}

    def test_asymmetric_returns_jwk(self, monkeypatch):
        monkeypatch.setenv("MCP_JWT_PRIVATE_KEY_PEM", _rsa_private_pem())
        monkeypatch.delenv("MCP_JWT_PUBLIC_KEY_PEM", raising=False)
        # Rebuild the process-wide auth singleton + settings so the
        # running app picks up the new env.
        from mcp_server.auth import reset_default_service
        from mcp_server.config import get_settings
        get_settings.cache_clear()
        reset_default_service()

        app = create_app(database_url="sqlite:///:memory:")
        with TestClient(app) as c:
            r = c.get("/.well-known/jwks.json")
            assert r.status_code == 200
            body = r.json()
            assert len(body["keys"]) == 1
            jwk = body["keys"][0]
            assert jwk["kty"] == "RSA"
            assert jwk["alg"] == "RS256"
            assert jwk["kid"] == "primary-rsa"
            assert jwk["n"]  # base64url
            assert jwk["e"]

        # Reset env + caches so subsequent tests stay on HMAC.
        monkeypatch.delenv("MCP_JWT_PRIVATE_KEY_PEM", raising=False)
        get_settings.cache_clear()
        reset_default_service()


class TestAsymmetricRoundTrip:
    def test_signs_and_verifies_with_rs256(self, monkeypatch):
        pem = _rsa_private_pem()
        monkeypatch.setenv("MCP_JWT_PRIVATE_KEY_PEM", pem)
        from mcp_server.auth import reset_default_service
        from mcp_server.config import get_settings
        get_settings.cache_clear()
        reset_default_service()

        settings = get_settings()
        ring = build_keyring(settings)
        assert ring.algorithm == "RS256"
        assert ring.active_kid == "primary-rsa"

        # Sign + verify directly with jose to prove the key is usable.
        claims = {"sub": "x", "iss": "mcp-server"}
        token = jwt.encode(
            claims, ring.active_secret, algorithm="RS256",
            headers={"kid": "primary-rsa"},
        )
        decoded = jwt.decode(
            token, ring.public_keys["primary-rsa"],
            algorithms=["RS256"], issuer="mcp-server",
        )
        assert decoded["sub"] == "x"

        monkeypatch.delenv("MCP_JWT_PRIVATE_KEY_PEM", raising=False)
        get_settings.cache_clear()
        reset_default_service()


class TestPrivateKeyFileInput:
    def test_pem_file_is_loaded(self, tmp_path, monkeypatch):
        pem = _rsa_private_pem()
        f = tmp_path / "priv.pem"
        f.write_text(pem)
        monkeypatch.setenv("MCP_JWT_PRIVATE_KEY_FILE", str(f))
        from mcp_server.config import get_settings
        get_settings.cache_clear()
        settings = get_settings()
        assert settings.jwt_private_key_pem == pem
        monkeypatch.delenv("MCP_JWT_PRIVATE_KEY_FILE", raising=False)
        get_settings.cache_clear()

    def test_missing_file_raises_runtime_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv(
            "MCP_JWT_PRIVATE_KEY_FILE", str(tmp_path / "nonexistent.pem")
        )
        from mcp_server.config import get_settings, Settings
        get_settings.cache_clear()
        with pytest.raises(RuntimeError, match="could not be read"):
            Settings()
        monkeypatch.delenv("MCP_JWT_PRIVATE_KEY_FILE", raising=False)
        get_settings.cache_clear()
