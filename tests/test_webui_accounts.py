"""Wave 9.5 Web UI — account switcher + accounts list + account detail.

Stubs the MCPClient so the tests stay fast and don't depend on a
live catalog process. For end-to-end account flows against the real
catalog, see tests/test_webui_client_accounts.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import webui.main as webui_main
from webui.main import create_app

from tests.conftest import TEST_OPERATOR_EMAIL, TEST_OPERATOR_PASSWORD


@pytest.fixture()
def mock_client(monkeypatch):
    fake = AsyncMock()
    fake.list_skills.return_value = []
    fake.list_skillsets.return_value = []
    fake.list_agents.return_value = []
    fake.list_accounts.return_value = []
    fake.list_members.return_value = []
    monkeypatch.setattr(webui_main, "_client", fake)
    monkeypatch.setattr(webui_main, "get_client", lambda: fake)
    return fake


@pytest.fixture()
def logged_in_client(mock_client, monkeypatch):
    """Log in as a regular (non-superadmin) user with a real user_id.

    The env-fallback `authenticate` sets user_id=None which breaks
    the Wave 9.5 membership lookups. Mock `authenticate_via_server`
    instead so the Operator has a proper id matching the mock's
    member rows.
    """
    from webui.auth import Operator

    async def _user_auth(email, password):
        return Operator(
            email=TEST_OPERATOR_EMAIL,
            role="admin",
            user_id="env-op",
            is_superadmin=False,
        )

    monkeypatch.setattr(webui_main, "authenticate_via_server", _user_auth)
    app = create_app()
    with TestClient(app) as c:
        r = c.post("/login", data={
            "email": TEST_OPERATOR_EMAIL,
            "password": TEST_OPERATOR_PASSWORD,
            "csrf_token": "", "next": "/",
        }, follow_redirects=False)
        assert r.status_code == 303
        yield c, mock_client


# ---------------------------------------------------------------------------
# /accounts list
# ---------------------------------------------------------------------------

class TestAccountsList:
    def test_anonymous_redirected_to_login(self, mock_client):
        app = create_app()
        with TestClient(app) as c:
            r = c.get("/accounts", follow_redirects=False)
            # AuthMiddleware kicks in first.
            assert r.status_code == 303
            assert r.headers["location"].startswith("/login?")

    def test_superadmin_sees_all_accounts(self, mock_client, monkeypatch):
        # Mock authenticate_via_server to return a superadmin Operator
        # since the real call tries to hit a running mcp_server process.
        from webui.auth import Operator

        async def _superadmin_auth(email, password):
            if email.strip().lower() == "superadmin@skillfulmcp.com":
                return Operator(
                    email="superadmin@skillfulmcp.com",
                    role="admin",
                    user_id="0",
                    is_superadmin=True,
                )
            return None

        monkeypatch.setattr(webui_main, "authenticate_via_server", _superadmin_auth)
        mock_client.list_accounts.return_value = [
            {"id": "a1", "name": "Team A", "created_at": "2026-04-13T00:00:00",
             "updated_at": "2026-04-13T00:00:00"},
            {"id": "a2", "name": "Team B", "created_at": "2026-04-13T00:00:00",
             "updated_at": "2026-04-13T00:00:00"},
        ]
        app = create_app()
        with TestClient(app) as c:
            c.post("/login", data={
                "email": "superadmin@skillfulmcp.com",
                "password": "anything",  # mock doesn't verify
                "csrf_token": "", "next": "/",
            }, follow_redirects=False)
            r = c.get("/accounts")
            assert r.status_code == 200
            assert b"Team A" in r.content
            assert b"Team B" in r.content
            assert b"SUPERADMIN" in r.content  # footer badge

    def test_regular_user_sees_only_memberships(self, logged_in_client):
        client, mock = logged_in_client
        mock.list_accounts.return_value = [
            {"id": "a1", "name": "In Scope", "created_at": "2026-04-13T00:00:00",
             "updated_at": "2026-04-13T00:00:00"},
            {"id": "a2", "name": "Not Mine", "created_at": "2026-04-13T00:00:00",
             "updated_at": "2026-04-13T00:00:00"},
        ]

        async def _list_members(aid):
            if aid == "a1":
                return [
                    {"user_id": "env-op", "email": TEST_OPERATOR_EMAIL,
                     "role": "account-admin", "pending": False,
                     "created_at": "2026-04-13T00:00:00"},
                ]
            return [
                {"user_id": "someone-else", "email": "x@y.com",
                 "role": "viewer", "pending": False,
                 "created_at": "2026-04-13T00:00:00"},
            ]
        mock.list_members.side_effect = _list_members
        r = client.get("/accounts")
        assert r.status_code == 200
        assert b"In Scope" in r.content
        assert b"Not Mine" not in r.content


# ---------------------------------------------------------------------------
# /accounts/new + POST /accounts
# ---------------------------------------------------------------------------

class TestCreateAccount:
    def test_form_renders_for_regular_user(self, logged_in_client):
        client, _ = logged_in_client
        r = client.get("/accounts/new")
        assert r.status_code == 200
        assert b"Create account" in r.content
        assert b'name="name"' in r.content

    def test_superadmin_redirect(self, mock_client, monkeypatch):
        """Superadmin can't become first account-admin — they aren't
        in the users table. Redirect with a clear error."""
        from webui.auth import Operator

        async def _superadmin_auth(email, password):
            return Operator(
                email="superadmin@skillfulmcp.com",
                role="admin", user_id="0", is_superadmin=True,
            )

        monkeypatch.setattr(webui_main, "authenticate_via_server", _superadmin_auth)
        app = create_app()
        with TestClient(app) as c:
            c.post("/login", data={
                "email": "superadmin@skillfulmcp.com",
                "password": "anything",
                "csrf_token": "", "next": "/",
            }, follow_redirects=False)
            r = c.get("/accounts/new", follow_redirects=False)
            assert r.status_code == 303
            assert "/accounts" in r.headers["location"]
            assert "msg_type=error" in r.headers["location"]

    def test_post_forwards_to_server_and_switches_account(
        self, logged_in_client
    ):
        client, mock = logged_in_client
        mock.create_account.return_value = {
            "id": "new-acc-id", "name": "New Team",
            "created_at": "2026-04-13T00:00:00",
            "updated_at": "2026-04-13T00:00:00",
        }
        r = client.post(
            "/accounts",
            data={"name": "New Team", "csrf_token": ""},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"].startswith("/accounts/new-acc-id")
        mock.create_account.assert_awaited_once()

    def test_post_server_error_redirects_back_with_flash(
        self, logged_in_client
    ):
        client, mock = logged_in_client
        from webui.client import MCPError
        mock.create_account.side_effect = MCPError("name in use", 409)
        r = client.post(
            "/accounts",
            data={"name": "Dup", "csrf_token": ""},
            follow_redirects=False,
        )
        assert r.status_code == 303
        loc = r.headers["location"]
        assert loc.startswith("/accounts/new")
        assert "msg_type=error" in loc


# ---------------------------------------------------------------------------
# /accounts/{id} detail + invite + role change
# ---------------------------------------------------------------------------

class TestAccountDetail:
    def test_renders_members_and_invite_form_for_admin(
        self, logged_in_client
    ):
        client, mock = logged_in_client
        mock.get_account.return_value = {
            "id": "a1", "name": "Team A",
            "created_at": "2026-04-13T00:00:00",
            "updated_at": "2026-04-13T00:00:00",
        }

        async def _list_members(aid):
            return [
                {"user_id": "env-op", "email": TEST_OPERATOR_EMAIL,
                 "role": "account-admin", "pending": False,
                 "created_at": "2026-04-13T00:00:00"},
                {"user_id": "u2", "email": "bob@x.com",
                 "role": "contributor", "pending": False,
                 "created_at": "2026-04-13T00:00:00"},
                {"id": 42, "email": "future@x.com", "role": "viewer",
                 "pending": True, "created_at": "2026-04-13T00:00:00",
                 "account_id": "a1"},
            ]
        mock.list_members.side_effect = _list_members
        mock.list_accounts.return_value = [
            {"id": "a1", "name": "Team A",
             "created_at": "2026-04-13T00:00:00",
             "updated_at": "2026-04-13T00:00:00"},
        ]
        r = client.get("/accounts/a1")
        assert r.status_code == 200
        assert b"Team A" in r.content
        assert b"bob@x.com" in r.content
        assert b"future@x.com" in r.content
        assert b"pending invite" in r.content
        # Admin sees the invite form.
        assert b'name="email"' in r.content

    def test_invite_forwards_to_server(self, logged_in_client):
        client, mock = logged_in_client
        mock.get_account.return_value = {
            "id": "a1", "name": "Team A",
            "created_at": "2026-04-13T00:00:00",
            "updated_at": "2026-04-13T00:00:00",
        }
        mock.list_members.return_value = []
        mock.list_accounts.return_value = [
            {"id": "a1", "name": "Team A",
             "created_at": "2026-04-13T00:00:00",
             "updated_at": "2026-04-13T00:00:00"}
        ]
        mock.invite_member.return_value = {}
        r = client.post(
            "/accounts/a1/members",
            data={
                "email": "new@x.com", "role": "contributor",
                "csrf_token": "",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        mock.invite_member.assert_awaited_once_with(
            "a1", email="new@x.com", role="contributor"
        )

    def test_role_change_forwards_to_server(self, logged_in_client):
        client, mock = logged_in_client
        mock.update_member_role.return_value = {}
        r = client.post(
            "/accounts/a1/members/u2/role",
            data={"role": "viewer", "csrf_token": ""},
            follow_redirects=False,
        )
        assert r.status_code == 303
        mock.update_member_role.assert_awaited_once_with("a1", "u2", "viewer")

    def test_remove_member_via_htmx(self, logged_in_client):
        client, mock = logged_in_client
        mock.remove_member.return_value = None
        r = client.delete(
            "/accounts/a1/members/u2",
        )
        assert r.status_code == 200
        mock.remove_member.assert_awaited_once_with("a1", "u2")

    def test_revoke_pending_via_htmx(self, logged_in_client):
        client, mock = logged_in_client
        mock.delete_pending_invite.return_value = None
        r = client.delete("/accounts/a1/pending/42")
        assert r.status_code == 200
        mock.delete_pending_invite.assert_awaited_once_with("a1", 42)


# ---------------------------------------------------------------------------
# /session/switch-account
# ---------------------------------------------------------------------------

class TestSessionSwitch:
    def test_switch_to_member_account_succeeds(self, logged_in_client):
        client, mock = logged_in_client
        mock.list_accounts.return_value = [
            {"id": "a1", "name": "Team A",
             "created_at": "2026-04-13T00:00:00",
             "updated_at": "2026-04-13T00:00:00"},
        ]

        async def _list_members(aid):
            return [{
                "user_id": "env-op", "email": TEST_OPERATOR_EMAIL,
                "role": "account-admin", "pending": False,
                "created_at": "2026-04-13T00:00:00",
            }]
        mock.list_members.side_effect = _list_members
        r = client.post(
            "/session/switch-account",
            data={"account_id": "a1", "csrf_token": ""},
            follow_redirects=False,
        )
        assert r.status_code == 303

    def test_switch_to_non_member_account_rejected(
        self, logged_in_client
    ):
        client, mock = logged_in_client
        mock.list_accounts.return_value = [
            {"id": "other", "name": "Other",
             "created_at": "2026-04-13T00:00:00",
             "updated_at": "2026-04-13T00:00:00"},
        ]
        mock.list_members.return_value = [
            {"user_id": "stranger", "email": "stranger@x.com",
             "role": "account-admin", "pending": False,
             "created_at": "2026-04-13T00:00:00"},
        ]
        r = client.post(
            "/session/switch-account",
            data={"account_id": "other", "csrf_token": ""},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert "msg_type=error" in r.headers["location"]
