"""
Wave 8c — Web UI token-issuance tests.

Stubs MCPClient and asserts:
- /agents renders the list + a Mint button for admins.
- /agents/{id}/tokens/new renders the wizard prefilled from the agent's
  grants.
- POST /agents/{id}/tokens forwards the narrowed payload and renders
  token_result.html with the token visible once.
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
    fake.list_skillsets.return_value = []
    fake.list_skills.return_value = []
    fake.list_agents.return_value = [
        {"id": "agent-1", "name": "Agent One",
         "skillsets": ["ss-a"], "skills": ["sk-x", "sk-y"], "scope": ["read"]},
    ]
    fake.get_agent.return_value = {
        "id": "agent-1", "name": "Agent One",
        "skillsets": ["ss-a"], "skills": ["sk-x", "sk-y"], "scope": ["read"],
    }
    fake.issue_token.return_value = {"access_token": "eyJabc.def.ghi",
                                     "token_type": "bearer",
                                     "expires_in": 3600}
    monkeypatch.setattr(webui_main, "_client", fake)
    monkeypatch.setattr(webui_main, "get_client", lambda: fake)
    return fake


@pytest.fixture()
def admin_client(mock_client):
    app = create_app()
    with TestClient(app) as c:
        r = c.post("/login", data={
            "email": TEST_OPERATOR_EMAIL,
            "password": TEST_OPERATOR_PASSWORD,
            "csrf_token": "", "next": "/",
        }, follow_redirects=False)
        assert r.status_code == 303
        yield c, mock_client


class TestTokenErrorPaths:
    def test_mint_form_404_when_agent_missing(self, mock_client, admin_client):
        client, mock = admin_client
        from webui.client import MCPError
        mock.get_agent.side_effect = MCPError("Agent not found", 404)
        r = client.get("/agents/missing/tokens/new", follow_redirects=False)
        # Redirects to /agents with error flash.
        assert r.status_code == 303
        assert "msg_type=error" in r.headers["location"]

    def test_mint_server_error_redirects_back(self, admin_client):
        client, mock = admin_client
        from webui.client import MCPError
        mock.issue_token.side_effect = MCPError("superset not allowed", 400)
        r = client.post("/agents/agent-1/tokens", data={
            "expires_in": "60",
            "_skills_present": "1", "skills": ["sk-x"],
            "_skillsets_present": "1",
            "_scope_present": "1", "scope": ["read"],
            "csrf_token": "",
        }, follow_redirects=False)
        assert r.status_code == 303
        assert "/agents/agent-1/tokens/new" in r.headers["location"]

    def test_agents_page_handles_server_error(self, admin_client):
        client, mock = admin_client
        from webui.client import MCPError
        mock.list_agents.side_effect = MCPError("boom", 500)
        r = client.get("/agents")
        assert r.status_code == 200
        assert b"boom" in r.content


class TestAgentsPage:
    def test_lists_agents_with_mint_button(self, admin_client):
        client, _ = admin_client
        r = client.get("/agents")
        assert r.status_code == 200
        assert b"agent-1" in r.content
        assert b"/agents/agent-1/tokens/new" in r.content

    def test_mint_form_prefilled_with_agent_grants(self, admin_client):
        client, _ = admin_client
        r = client.get("/agents/agent-1/tokens/new")
        assert r.status_code == 200
        # Each grant rendered as a checkbox with its value.
        assert b'value="ss-a"' in r.content
        assert b'value="sk-x"' in r.content
        assert b'value="sk-y"' in r.content
        assert b'value="read"' in r.content

    def test_mint_full_grants(self, admin_client):
        client, mock = admin_client
        r = client.post("/agents/agent-1/tokens", data={
            "expires_in": "3600",
            "_skills_present": "1", "skills": ["sk-x", "sk-y"],
            "_skillsets_present": "1", "skillsets": ["ss-a"],
            "_scope_present": "1", "scope": ["read"],
            "csrf_token": "",
        })
        assert r.status_code == 200
        assert b"eyJabc.def.ghi" in r.content
        payload = mock.issue_token.await_args.args[0]
        assert payload["agent_id"] == "agent-1"
        assert payload["expires_in"] == 3600
        assert payload["skills"] == ["sk-x", "sk-y"]

    def test_mint_narrowed(self, admin_client):
        client, mock = admin_client
        # `_skillsets_present` marker is sent without any skillset values —
        # the form's "user unchecked all" signal.
        client.post("/agents/agent-1/tokens", data={
            "expires_in": "300",
            "_skills_present": "1", "skills": ["sk-x"],
            "_skillsets_present": "1",
            "_scope_present": "1", "scope": ["read"],
            "csrf_token": "",
        })
        payload = mock.issue_token.await_args.args[0]
        assert payload["skills"] == ["sk-x"]
        assert payload["skillsets"] == []
