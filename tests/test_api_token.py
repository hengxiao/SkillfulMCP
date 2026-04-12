"""Integration tests for POST /token."""

from tests.conftest import ADMIN_HEADERS, make_agent


class TestTokenEndpoint:
    def test_issue_token_for_existing_agent(self, client):
        make_agent(client)
        r = client.post(
            "/token",
            json={"agent_id": "agent-1", "expires_in": 3600},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 200
        data = r.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"
        assert data["expires_in"] == 3600

    def test_token_is_a_valid_jwt(self, client):
        from mcp_server.auth import validate_token
        make_agent(client)
        r = client.post(
            "/token",
            json={"agent_id": "agent-1", "expires_in": 3600},
            headers=ADMIN_HEADERS,
        )
        token = r.json()["access_token"]
        claims = validate_token(token)
        assert claims["sub"] == "agent-1"

    def test_agent_not_found_returns_404(self, client):
        r = client.post(
            "/token",
            json={"agent_id": "ghost-agent", "expires_in": 3600},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 404

    def test_missing_admin_key_returns_403(self, client):
        make_agent(client)
        r = client.post(
            "/token",
            json={"agent_id": "agent-1", "expires_in": 3600},
        )
        assert r.status_code == 403

    def test_wrong_admin_key_returns_403(self, client):
        make_agent(client)
        r = client.post(
            "/token",
            json={"agent_id": "agent-1", "expires_in": 3600},
            headers={"X-Admin-Key": "wrong-key"},
        )
        assert r.status_code == 403

    def test_default_expires_in(self, client):
        make_agent(client)
        r = client.post(
            "/token",
            json={"agent_id": "agent-1"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 200
        assert r.json()["expires_in"] == 3600
