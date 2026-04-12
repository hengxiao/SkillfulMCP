"""Integration tests for the /agents API endpoints."""

from tests.conftest import ADMIN_HEADERS, make_agent


class TestListAgents:
    def test_empty_initially(self, client):
        r = client.get("/agents", headers=ADMIN_HEADERS)
        assert r.status_code == 200
        assert r.json() == []

    def test_lists_created_agents(self, client):
        make_agent(client, id="a-1")
        make_agent(client, id="a-2")
        r = client.get("/agents", headers=ADMIN_HEADERS)
        ids = [a["id"] for a in r.json()]
        assert set(ids) == {"a-1", "a-2"}

    def test_requires_admin_key(self, client):
        r = client.get("/agents")
        assert r.status_code == 403


class TestGetAgent:
    def test_get_existing(self, client):
        make_agent(client, id="a-1", skillsets=["ss-x"], scope=["read", "execute"])
        r = client.get("/agents/a-1", headers=ADMIN_HEADERS)
        assert r.status_code == 200
        data = r.json()
        assert data["id"] == "a-1"
        assert set(data["scope"]) == {"read", "execute"}

    def test_not_found(self, client):
        r = client.get("/agents/ghost", headers=ADMIN_HEADERS)
        assert r.status_code == 404

    def test_response_shape(self, client):
        make_agent(client)
        data = client.get("/agents/agent-1", headers=ADMIN_HEADERS).json()
        for field in ("id", "name", "skillsets", "skills", "scope", "created_at", "updated_at"):
            assert field in data


class TestCreateAgent:
    def test_creates_agent(self, client):
        r = client.post(
            "/agents",
            json={"id": "new-agent", "name": "New", "scope": ["read"]},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 201
        assert r.json()["id"] == "new-agent"

    def test_duplicate_returns_409(self, client):
        make_agent(client)
        r = client.post(
            "/agents",
            json={"id": "agent-1", "name": "Dup"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 409

    def test_invalid_scope_returns_422(self, client):
        r = client.post(
            "/agents",
            json={"id": "x", "name": "X", "scope": ["admin"]},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 422

    def test_requires_admin_key(self, client):
        r = client.post("/agents", json={"id": "x", "name": "X"})
        assert r.status_code == 403


class TestUpdateAgent:
    def test_updates_name(self, client):
        make_agent(client)
        r = client.put(
            "/agents/agent-1",
            json={"name": "Renamed"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 200
        assert r.json()["name"] == "Renamed"

    def test_updates_scope(self, client):
        make_agent(client, scope=["read"])
        r = client.put(
            "/agents/agent-1",
            json={"scope": ["read", "execute"]},
            headers=ADMIN_HEADERS,
        )
        assert set(r.json()["scope"]) == {"read", "execute"}

    def test_partial_update_preserves_other_fields(self, client):
        make_agent(client, skillsets=["ss-1", "ss-2"])
        r = client.put(
            "/agents/agent-1",
            json={"name": "Only Name Changed"},
            headers=ADMIN_HEADERS,
        )
        assert set(r.json()["skillsets"]) == {"ss-1", "ss-2"}

    def test_not_found_returns_404(self, client):
        r = client.put("/agents/ghost", json={"name": "X"}, headers=ADMIN_HEADERS)
        assert r.status_code == 404

    def test_invalid_scope_returns_422(self, client):
        make_agent(client)
        r = client.put(
            "/agents/agent-1",
            json={"scope": ["superadmin"]},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 422


class TestDeleteAgent:
    def test_deletes_agent(self, client):
        make_agent(client)
        r = client.delete("/agents/agent-1", headers=ADMIN_HEADERS)
        assert r.status_code == 204

    def test_not_found_returns_404(self, client):
        r = client.delete("/agents/ghost", headers=ADMIN_HEADERS)
        assert r.status_code == 404

    def test_requires_admin_key(self, client):
        make_agent(client)
        r = client.delete("/agents/agent-1")
        assert r.status_code == 403
