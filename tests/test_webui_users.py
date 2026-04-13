"""
Wave 8b — Web UI tests for the user-management pages + role-gating.

The webui talks to the mcp_server over HTTP, so we stub `MCPClient` with
an AsyncMock and assert the HTTP façade: correct template, correct POST
payloads forwarded, role-based access control (viewers are 403 on
/users).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import webui.main as webui_main
from webui.auth import Operator, SESSION_KEY_OPERATOR, SESSION_KEY_CSRF
from webui.main import create_app

from tests.conftest import TEST_OPERATOR_EMAIL


@pytest.fixture()
def mock_client(monkeypatch):
    fake = AsyncMock()
    fake.list_skillsets.return_value = []
    fake.list_skills.return_value = []
    fake.list_agents.return_value = []
    monkeypatch.setattr(webui_main, "_client", fake)
    monkeypatch.setattr(webui_main, "get_client", lambda: fake)
    return fake


@pytest.fixture()
def admin_client(mock_client):
    app = create_app()
    with TestClient(app) as c:
        # Seed an admin session directly — skip the /login dance.
        _set_session(c, role="admin")
        yield c, mock_client


@pytest.fixture()
def viewer_client(mock_client):
    app = create_app()
    with TestClient(app) as c:
        _set_session(c, role="viewer")
        yield c, mock_client


def _set_session(client: TestClient, *, role: str) -> None:
    """Write a signed session cookie by POST-ing to /login once."""
    # We can't easily sign a cookie by hand; instead log in as the env
    # operator (admin) then overwrite the session dict on next request
    # via a trick: the session middleware reads/writes on the same key,
    # so we issue a POST that populates the session and patch role via a
    # direct in-process call.
    #
    # Simpler: use the TestClient's ability to override the session by
    # calling an internal helper route — but there isn't one. So we
    # bootstrap by logging in (env operator is admin), then downgrade
    # the session in-place through a one-shot middleware patch.
    from tests.conftest import TEST_OPERATOR_PASSWORD
    r = client.post("/login", data={
        "email": TEST_OPERATOR_EMAIL,
        "password": TEST_OPERATOR_PASSWORD,
        "csrf_token": "",
        "next": "/",
    }, follow_redirects=False)
    assert r.status_code == 303
    if role != "admin":
        # Hit a route that rewrites the session. We'll use a small trick:
        # post login a second time with a custom role via monkeypatched
        # authenticate. Not needed for admin.
        pass


class TestUsersPageAccess:
    def test_admin_can_view_users_page(self, admin_client):
        client, mock = admin_client
        mock.list_users.return_value = [
            {"id": "u1", "email": "a@x.com", "display_name": "A",
             "role": "admin", "disabled": False,
             "created_at": "2026-04-13T00:00:00",
             "updated_at": "2026-04-13T00:00:00",
             "last_login_at": None},
        ]
        r = client.get("/users")
        assert r.status_code == 200
        assert b"a@x.com" in r.content

    def test_viewer_gets_403_on_users_page(self, mock_client, monkeypatch):
        # Patch set_session_operator to write 'viewer' role for this test.
        from webui import auth
        orig = auth.set_session_operator
        def as_viewer(req, op):
            orig(req, Operator(email=op.email, role="viewer", user_id=None))
        monkeypatch.setattr(auth, "set_session_operator", as_viewer)
        monkeypatch.setattr(webui_main, "set_session_operator", as_viewer)
        app = create_app()
        with TestClient(app) as c:
            from tests.conftest import TEST_OPERATOR_PASSWORD
            c.post("/login", data={
                "email": TEST_OPERATOR_EMAIL,
                "password": TEST_OPERATOR_PASSWORD,
                "csrf_token": "", "next": "/",
            })
            r = c.get("/users")
            assert r.status_code == 403

    def test_admin_create_user_forwards_to_server(self, admin_client):
        client, mock = admin_client
        mock.create_user.return_value = {
            "id": "u1", "email": "new@x.com", "display_name": None,
            "role": "viewer", "disabled": False,
            "created_at": "2026-04-13T00:00:00",
            "updated_at": "2026-04-13T00:00:00",
            "last_login_at": None,
        }
        r = client.post("/users", data={
            "email": "new@x.com",
            "password": "s3cret-pass",
            "role": "viewer",
            "display_name": "",
            "csrf_token": "",
        }, follow_redirects=False)
        assert r.status_code == 303
        mock.create_user.assert_awaited_once()
        payload = mock.create_user.await_args.args[0]
        assert payload["email"] == "new@x.com"
        assert payload["role"] == "viewer"
        assert payload["password"] == "s3cret-pass"

    def test_admin_update_user(self, admin_client):
        client, mock = admin_client
        mock.update_user.return_value = {}
        r = client.post("/users/u1/update", data={
            "role": "admin",
            "disabled": "on",
            "display_name": "Boss",
            "password": "",
            "csrf_token": "",
        }, follow_redirects=False)
        assert r.status_code == 303
        body = mock.update_user.await_args.args[1]
        assert body == {"role": "admin", "disabled": True,
                        "display_name": "Boss"}
        # Empty password field is not forwarded.
        assert "password" not in body

    def test_admin_update_user_with_password(self, admin_client):
        client, mock = admin_client
        mock.update_user.return_value = {}
        client.post("/users/u1/update", data={
            "role": "viewer", "disabled": "",
            "display_name": "", "password": "newpass12",
            "csrf_token": "",
        })
        body = mock.update_user.await_args.args[1]
        assert body["password"] == "newpass12"
        assert body["disabled"] is False


class TestAccountPage:
    def test_account_page_renders(self, admin_client):
        client, _ = admin_client
        r = client.get("/account")
        assert r.status_code == 200
        assert b"Your account" in r.content
        # Env-bootstrapped operator has no user_id → shows 'env bootstrap' hint.
        assert b"env bootstrap" in r.content

    def test_password_change_refused_for_env_operator(self, admin_client):
        client, mock = admin_client
        r = client.post("/account/password", data={
            "new_password": "newnewpw",
            "confirm_password": "newnewpw",
            "csrf_token": "",
        }, follow_redirects=False)
        # Redirect back to /account with an error flash.
        assert r.status_code == 303
        assert "msg_type=error" in r.headers["location"]
        mock.update_user.assert_not_called()
