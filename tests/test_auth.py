"""Tests for JWT issuance and validation (mcp_server.auth)."""

import time
from types import SimpleNamespace

import pytest
from jose import jwt as jose_jwt

from mcp_server.auth import issue_token, validate_token
from mcp_server.config import get_settings


def _make_agent(**kwargs) -> SimpleNamespace:
    defaults = dict(
        id="agent-test",
        name="Test Agent",
        skillsets=["ss-1"],
        skills=["skill-a"],
        scope=["read"],
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class TestIssueToken:
    def test_returns_string(self):
        agent = _make_agent()
        token = issue_token(agent)
        assert isinstance(token, str)
        assert token.count(".") == 2  # header.payload.signature

    def test_claims_sub_and_iss(self):
        agent = _make_agent(id="my-agent")
        token = issue_token(agent)
        claims = validate_token(token)
        assert claims["sub"] == "my-agent"
        assert claims["iss"] == get_settings().jwt_issuer

    def test_skillsets_embedded(self):
        agent = _make_agent(skillsets=["ss-a", "ss-b"])
        claims = validate_token(issue_token(agent))
        assert claims["skillsets"] == ["ss-a", "ss-b"]

    def test_skills_embedded(self):
        agent = _make_agent(skills=["skill-x", "skill-y"])
        claims = validate_token(issue_token(agent))
        assert set(claims["skills"]) == {"skill-x", "skill-y"}

    def test_scope_embedded_as_list(self):
        agent = _make_agent(scope=["read", "execute"])
        claims = validate_token(issue_token(agent))
        assert set(claims["scope"]) == {"read", "execute"}

    def test_default_expiry_is_in_future(self):
        agent = _make_agent()
        token = issue_token(agent, expires_in=3600)
        claims = validate_token(token)
        assert claims["exp"] > int(time.time())

    def test_custom_expiry(self):
        agent = _make_agent()
        token = issue_token(agent, expires_in=7200)
        settings = get_settings()
        raw = jose_jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm],
            options={"verify_exp": False},
        )
        assert raw["exp"] - raw["iat"] == 7200

    def test_empty_skillsets_ok(self):
        agent = _make_agent(skillsets=[], skills=[], scope=[])
        token = issue_token(agent)
        claims = validate_token(token)
        assert claims["skillsets"] == []
        assert claims["skills"] == []
        assert claims["scope"] == []


class TestValidateToken:
    def test_expired_token_raises_http_401(self):
        from datetime import datetime, timedelta, timezone

        from fastapi import HTTPException
        from jose import jwt

        from mcp_server.config import get_settings

        # issue_token clamps expires_in to >= 1 (negative is nonsense). To
        # test expired-token handling we hand-build a JWT with a past `exp`.
        settings = get_settings()
        past = datetime.now(timezone.utc) - timedelta(seconds=60)
        token = jwt.encode(
            {
                "sub": "agent-test",
                "iss": settings.jwt_issuer,
                "iat": int((past - timedelta(seconds=1)).timestamp()),
                "exp": int(past.timestamp()),
                "jti": "expired-jti",
                "skillsets": [], "skills": [], "scope": [],
            },
            settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
            headers={"kid": "primary"},
        )
        with pytest.raises(HTTPException) as exc_info:
            validate_token(token)
        assert exc_info.value.status_code == 401

    def test_tampered_signature_raises_http_401(self):
        from fastapi import HTTPException
        agent = _make_agent()
        token = issue_token(agent)
        bad_token = token[:-4] + "XXXX"
        with pytest.raises(HTTPException) as exc_info:
            validate_token(bad_token)
        assert exc_info.value.status_code == 401

    def test_wrong_secret_raises_http_401(self):
        from fastapi import HTTPException
        agent = _make_agent()
        # Manually encode with a different secret
        import time as _time
        claims = {
            "sub": agent.id, "iss": "mcp-server",
            "iat": int(_time.time()), "exp": int(_time.time()) + 3600,
            "skillsets": [], "skills": [], "scope": [],
        }
        bad_token = jose_jwt.encode(claims, "wrong-secret", algorithm="HS256")
        with pytest.raises(HTTPException) as exc_info:
            validate_token(bad_token)
        assert exc_info.value.status_code == 401

    def test_wrong_issuer_raises_http_401(self):
        from fastapi import HTTPException
        import time as _time
        settings = get_settings()
        claims = {
            "sub": "x", "iss": "not-mcp-server",
            "iat": int(_time.time()), "exp": int(_time.time()) + 3600,
            "skillsets": [], "skills": [], "scope": [],
        }
        token = jose_jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)
        with pytest.raises(HTTPException) as exc_info:
            validate_token(token)
        assert exc_info.value.status_code == 401

    def test_malformed_token_raises_http_401(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            validate_token("not.a.jwt")
        assert exc_info.value.status_code == 401
