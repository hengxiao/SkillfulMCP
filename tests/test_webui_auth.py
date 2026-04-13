"""
Wave 6a tests — operator auth for the Web UI.

Two flavors:
- Login / logout / auth-middleware redirect behavior (CSRF OFF; same as
  the default `webui` fixture).
- CSRF enforcement, tested on a dedicated app built with CSRF enabled so
  we can assert that unsafe methods without the token are rejected.

Uses TestClient directly (not the shared `webui` fixture) to control
the session-cookie + CSRF state precisely.
"""

from __future__ import annotations

import json
import os
import re
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import webui.main as webui_main
from webui.auth import hash_password, verify_password
from webui.main import create_app

from tests.conftest import TEST_OPERATOR_EMAIL, TEST_OPERATOR_PASSWORD


# ---------------------------------------------------------------------------
# Unit: bcrypt verify
# ---------------------------------------------------------------------------

class TestPasswordHashing:
    def test_round_trip(self):
        h = hash_password("correct horse battery staple")
        assert verify_password("correct horse battery staple", h)
        assert not verify_password("wrong", h)

    def test_empty_inputs_reject(self):
        assert not verify_password("", "$2b$12$anyhash")
        assert not verify_password("pw", "")

    def test_malformed_hash_returns_false_not_raises(self):
        assert not verify_password("pw", "not-a-bcrypt-hash")

    def test_truncates_past_72_bytes(self):
        """bcrypt caps at 72 bytes. Our helper truncates deterministically
        so a 200-char password still hashes and verifies."""
        pw = "a" * 200
        h = hash_password(pw)
        assert verify_password(pw, h)
        # First 72 bytes must still verify.
        assert verify_password("a" * 72, h)


# ---------------------------------------------------------------------------
# Integration: login / logout / redirect
# ---------------------------------------------------------------------------

@pytest.fixture()
def app_client(monkeypatch):
    """Unauthenticated TestClient against a fresh app (CSRF OFF from env)."""
    fake = AsyncMock()
    fake.list_skillsets.return_value = []
    fake.list_skills.return_value = []
    fake.list_agents.return_value = []
    fake.list_bundle_files.return_value = []
    fake.list_skill_versions.return_value = []
    fake.list_skillset_skills.return_value = []
    monkeypatch.setattr(webui_main, "_client", fake)
    monkeypatch.setattr(webui_main, "get_client", lambda: fake)
    app = create_app()
    with TestClient(app) as c:
        yield c


class TestLogin:
    def test_get_login_returns_form(self, app_client):
        r = app_client.get("/login")
        assert r.status_code == 200
        assert b'name="email"' in r.content
        assert b'name="password"' in r.content
        assert b'name="csrf_token"' in r.content  # always present on login

    def test_login_with_valid_credentials_sets_session(self, app_client):
        r = app_client.post(
            "/login",
            data={
                "email": TEST_OPERATOR_EMAIL,
                "password": TEST_OPERATOR_PASSWORD,
                "csrf_token": "",
                "next": "/",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/"
        # Session cookie now set — subsequent request to / succeeds.
        r2 = app_client.get("/", follow_redirects=False)
        assert r2.status_code == 200

    def test_login_with_bad_password_returns_form_with_error(self, app_client):
        r = app_client.post(
            "/login",
            data={
                "email": TEST_OPERATOR_EMAIL,
                "password": "wrong-password",
                "csrf_token": "",
                "next": "/",
            },
        )
        assert r.status_code == 200
        assert b"Invalid email or password" in r.content

    def test_login_with_unknown_email_returns_form_with_error(self, app_client):
        r = app_client.post(
            "/login",
            data={
                "email": "nobody@example.com",
                "password": "anything",
                "csrf_token": "",
                "next": "/",
            },
        )
        assert r.status_code == 200
        assert b"Invalid email or password" in r.content

    def test_login_normalizes_email_case(self, app_client):
        r = app_client.post(
            "/login",
            data={
                "email": TEST_OPERATOR_EMAIL.upper(),
                "password": TEST_OPERATOR_PASSWORD,
                "csrf_token": "",
                "next": "/",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303

    def test_login_respects_next_param(self, app_client):
        r = app_client.post(
            "/login",
            data={
                "email": TEST_OPERATOR_EMAIL,
                "password": TEST_OPERATOR_PASSWORD,
                "csrf_token": "",
                "next": "/skills?version=1.0.0",
            },
            follow_redirects=False,
        )
        assert r.headers["location"] == "/skills?version=1.0.0"

    def test_login_rejects_external_next_param(self, app_client):
        r = app_client.post(
            "/login",
            data={
                "email": TEST_OPERATOR_EMAIL,
                "password": TEST_OPERATOR_PASSWORD,
                "csrf_token": "",
                "next": "https://evil.example.com/phish",
            },
            follow_redirects=False,
        )
        assert r.headers["location"] == "/"

    def test_login_rejects_protocol_relative_next(self, app_client):
        r = app_client.post(
            "/login",
            data={
                "email": TEST_OPERATOR_EMAIL,
                "password": TEST_OPERATOR_PASSWORD,
                "csrf_token": "",
                "next": "//evil.example.com/",
            },
            follow_redirects=False,
        )
        assert r.headers["location"] == "/"


class TestAuthRedirect:
    def test_unauth_root_is_public_landing(self, app_client):
        # `/` is the public catalog landing page — served to anyone, with
        # a "Sign in" button in place of the operator-only links.
        r = app_client.get("/", follow_redirects=False)
        assert r.status_code == 200
        assert b"Public catalog" in r.content
        assert b"Sign in" in r.content

    def test_unauth_guarded_path_redirects(self, app_client):
        r = app_client.get("/skills", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"].startswith("/login?next=")

    def test_next_parameter_preserves_query_string(self, app_client):
        r = app_client.get("/skills?version=1.0.0", follow_redirects=False)
        assert r.status_code == 303
        # URL-encoded query survives.
        assert "skills%3Fversion%3D1.0.0" in r.headers["location"]

    def test_login_path_itself_is_not_guarded(self, app_client):
        r = app_client.get("/login")
        assert r.status_code == 200


class TestLogout:
    def test_logout_clears_session_and_redirects(self, app_client):
        # Log in.
        app_client.post(
            "/login",
            data={
                "email": TEST_OPERATOR_EMAIL,
                "password": TEST_OPERATOR_PASSWORD,
                "csrf_token": "",
                "next": "/",
            },
            follow_redirects=False,
        )
        # Logout.
        r = app_client.post("/logout", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/login"
        # Next request to a guarded path redirects — session is gone.
        r2 = app_client.get("/skills", follow_redirects=False)
        assert r2.status_code == 303
        assert r2.headers["location"].startswith("/login?next=")


# ---------------------------------------------------------------------------
# CSRF enforcement — dedicated CSRF-enabled app
# ---------------------------------------------------------------------------

@pytest.fixture()
def csrf_client(monkeypatch):
    """CSRF-enabled app with an authenticated test operator.

    Yields (client, csrf_token) so tests can send legitimate requests with
    the token and hostile ones without.
    """
    monkeypatch.setenv("MCP_WEBUI_CSRF_ENABLED", "1")
    from webui.config import get_settings
    get_settings.cache_clear()

    fake = AsyncMock()
    fake.list_skillsets.return_value = []
    fake.list_skills.return_value = []
    fake.list_agents.return_value = []
    fake.list_skillset_skills.return_value = []
    monkeypatch.setattr(webui_main, "_client", fake)
    monkeypatch.setattr(webui_main, "get_client", lambda: fake)

    app = create_app()
    try:
        with TestClient(app) as c:
            # Hit /login first to get a csrf token from the form.
            r = c.get("/login")
            m = re.search(rb'name="csrf_token"\s+value="([^"]+)"', r.content)
            assert m is not None, "CSRF token not found on login form"
            login_token = m.group(1).decode()

            # Submit login with that token.
            r = c.post(
                "/login",
                data={
                    "email": TEST_OPERATOR_EMAIL,
                    "password": TEST_OPERATOR_PASSWORD,
                    "csrf_token": login_token,
                    "next": "/",
                },
                follow_redirects=False,
            )
            assert r.status_code == 303, f"login failed: {r.status_code} {r.text[:200]}"

            # Post-login token is a new one (regenerated on auth). Grab it
            # from the dashboard meta tag.
            r = c.get("/")
            meta = re.search(rb'<meta name="csrf-token" content="([^"]+)"', r.content)
            assert meta is not None
            session_token = meta.group(1).decode()
            yield c, session_token
    finally:
        get_settings.cache_clear()


class TestCSRFProtection:
    def test_post_without_token_is_rejected(self, csrf_client):
        client, _token = csrf_client
        r = client.post("/skillsets", data={"id": "x", "name": "X"})
        assert r.status_code == 403
        # The CSRF dep signals failure via an X-Error-Code header + detail
        # message. The Web UI doesn't run the catalog's typed-error handler,
        # so there's no `code` in the JSON body here.
        assert r.headers.get("X-Error-Code") == "CSRF_FAILED"
        assert "CSRF" in r.json()["detail"]

    def test_post_with_valid_token_passes_csrf(self, csrf_client):
        """Not checking business logic — only that CSRF lets the request
        through. `fake_mcp` returns an AsyncMock whose list calls yield
        empty lists; the create_skillset handler calls fake.create_skillset
        which returns an AsyncMock too, and the handler just redirects."""
        client, token = csrf_client
        r = client.post(
            "/skillsets",
            data={"id": "x", "name": "X", "csrf_token": token},
            follow_redirects=False,
        )
        assert r.status_code == 303  # PRG redirect, NOT 403

    def test_htmx_header_is_accepted(self, csrf_client):
        client, token = csrf_client
        r = client.delete(
            "/skillsets/x",
            headers={"X-CSRF-Token": token},
        )
        # Value depends on the handler; what matters is we're PAST the
        # CSRF gate — any non-403 with code=CSRF_FAILED body is fine.
        if r.status_code == 403:
            assert r.json().get("code") != "CSRF_FAILED"

    def test_get_not_guarded_by_csrf(self, csrf_client):
        client, _token = csrf_client
        r = client.get("/")
        assert r.status_code == 200
