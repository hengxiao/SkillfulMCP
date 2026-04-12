"""Integration tests for the /skills API endpoints."""

import pytest

from tests.conftest import (
    ADMIN_HEADERS,
    bearer,
    get_token,
    make_agent,
    make_skill,
    make_skillset,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _auth_headers(client, *, skillsets=None, skills=None, scope=None):
    """Create an agent, get its token, and return bearer headers."""
    make_agent(
        client,
        id="api-agent",
        skillsets=skillsets or [],
        skills=skills or [],
        scope=scope or ["read"],
    )
    token = get_token(client, "api-agent")
    return bearer(token)


# ---------------------------------------------------------------------------
# GET /skills
# ---------------------------------------------------------------------------

class TestListSkills:
    def test_returns_empty_when_no_skills_authorized(self, client):
        headers = _auth_headers(client)
        r = client.get("/skills", headers=headers)
        assert r.status_code == 200
        assert r.json() == []

    def test_returns_authorized_skills(self, client):
        make_skill(client, id="skill-a")
        headers = _auth_headers(client, skills=["skill-a"])
        r = client.get("/skills", headers=headers)
        assert r.status_code == 200
        ids = [s["id"] for s in r.json()]
        assert "skill-a" in ids

    def test_does_not_return_unauthorized_skills(self, client):
        make_skill(client, id="skill-a")
        make_skill(client, id="skill-b")
        headers = _auth_headers(client, skills=["skill-a"])
        r = client.get("/skills", headers=headers)
        ids = [s["id"] for s in r.json()]
        assert "skill-b" not in ids

    def test_skillset_grants_access(self, client):
        make_skillset(client, id="ss-1")
        make_skill(client, id="skill-a", skillset_ids=["ss-1"])
        headers = _auth_headers(client, skillsets=["ss-1"])
        r = client.get("/skills", headers=headers)
        ids = [s["id"] for s in r.json()]
        assert "skill-a" in ids

    def test_returns_only_latest_version(self, client):
        make_skill(client, id="skill-a", version="1.0.0")
        make_skill(client, id="skill-a", version="2.0.0")
        headers = _auth_headers(client, skills=["skill-a"])
        r = client.get("/skills", headers=headers)
        versions = [s["version"] for s in r.json() if s["id"] == "skill-a"]
        assert versions == ["2.0.0"]

    def test_requires_auth(self, client):
        r = client.get("/skills")
        assert r.status_code in (401, 403)

    def test_invalid_token_returns_401(self, client):
        r = client.get("/skills", headers={"Authorization": "Bearer not-a-token"})
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# GET /skills/{skill_id}
# ---------------------------------------------------------------------------

class TestGetSkill:
    def test_get_latest(self, client):
        make_skill(client, id="skill-a", version="1.0.0")
        make_skill(client, id="skill-a", version="2.0.0")
        headers = _auth_headers(client, skills=["skill-a"])
        r = client.get("/skills/skill-a", headers=headers)
        assert r.status_code == 200
        assert r.json()["version"] == "2.0.0"
        assert r.json()["is_latest"] is True

    def test_get_specific_version(self, client):
        make_skill(client, id="skill-a", version="1.0.0")
        make_skill(client, id="skill-a", version="2.0.0")
        headers = _auth_headers(client, skills=["skill-a"])
        r = client.get("/skills/skill-a?version=1.0.0", headers=headers)
        assert r.status_code == 200
        assert r.json()["version"] == "1.0.0"

    def test_not_found_returns_404(self, client):
        headers = _auth_headers(client, skills=["skill-a"])
        r = client.get("/skills/skill-a", headers=headers)
        assert r.status_code == 404

    def test_unauthorized_skill_returns_403(self, client):
        make_skill(client, id="skill-a")
        headers = _auth_headers(client)  # no skills granted
        r = client.get("/skills/skill-a", headers=headers)
        assert r.status_code == 403

    def test_response_shape(self, client):
        make_skill(client, id="skill-a")
        headers = _auth_headers(client, skills=["skill-a"])
        data = client.get("/skills/skill-a", headers=headers).json()
        for field in ("id", "name", "description", "version", "is_latest", "metadata", "created_at", "updated_at"):
            assert field in data


# ---------------------------------------------------------------------------
# GET /skills/{skill_id}/versions
# ---------------------------------------------------------------------------

class TestListVersions:
    def test_lists_all_versions(self, client):
        for v in ("1.0.0", "1.1.0", "2.0.0"):
            make_skill(client, id="skill-a", version=v)
        headers = _auth_headers(client, skills=["skill-a"])
        r = client.get("/skills/skill-a/versions", headers=headers)
        assert r.status_code == 200
        versions = [v["version"] for v in r.json()]
        assert set(versions) == {"1.0.0", "1.1.0", "2.0.0"}

    def test_exactly_one_is_latest(self, client):
        for v in ("1.0.0", "2.0.0"):
            make_skill(client, id="skill-a", version=v)
        headers = _auth_headers(client, skills=["skill-a"])
        r = client.get("/skills/skill-a/versions", headers=headers)
        latest_flags = [v["is_latest"] for v in r.json()]
        assert latest_flags.count(True) == 1

    def test_unauthorized_returns_403(self, client):
        make_skill(client, id="skill-a")
        headers = _auth_headers(client)
        r = client.get("/skills/skill-a/versions", headers=headers)
        assert r.status_code == 403

    def test_nonexistent_returns_404(self, client):
        headers = _auth_headers(client, skills=["no-such"])
        r = client.get("/skills/no-such/versions", headers=headers)
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /skills (admin)
# ---------------------------------------------------------------------------

class TestCreateSkill:
    def test_creates_skill(self, client):
        r = client.post(
            "/skills",
            json={"id": "skill-new", "name": "New", "version": "1.0.0", "metadata": {}},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 201
        assert r.json()["id"] == "skill-new"

    def test_duplicate_returns_409(self, client):
        make_skill(client)
        r = client.post(
            "/skills",
            json={"id": "skill-a", "name": "A", "version": "1.0.0"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 409

    def test_invalid_semver_returns_422(self, client):
        r = client.post(
            "/skills",
            json={"id": "s", "name": "S", "version": "not-semver"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 422

    def test_non_dict_metadata_returns_422(self, client):
        r = client.post(
            "/skills",
            json={"id": "s", "name": "S", "version": "1.0.0", "metadata": "string"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 422

    def test_requires_admin_key(self, client):
        r = client.post(
            "/skills",
            json={"id": "s", "name": "S", "version": "1.0.0"},
        )
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# PUT /skills/{skill_id} (admin)
# ---------------------------------------------------------------------------

class TestUpsertSkill:
    def test_creates_if_absent(self, client):
        r = client.put(
            "/skills/skill-new",
            json={"name": "New", "version": "1.0.0", "metadata": {}},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 200
        assert r.json()["id"] == "skill-new"

    def test_updates_if_present(self, client):
        make_skill(client, id="skill-a", version="1.0.0")
        r = client.put(
            "/skills/skill-a",
            json={"name": "Updated Name", "version": "1.0.0", "description": "new"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 200
        assert r.json()["name"] == "Updated Name"

    def test_new_version_updates_latest(self, client):
        make_skill(client, id="skill-a", version="1.0.0")
        client.put(
            "/skills/skill-a",
            json={"name": "A", "version": "2.0.0"},
            headers=ADMIN_HEADERS,
        )
        # Now token-auth to verify
        headers = _auth_headers(client, skills=["skill-a"])
        r = client.get("/skills/skill-a", headers=headers)
        assert r.json()["version"] == "2.0.0"


# ---------------------------------------------------------------------------
# DELETE /skills/{skill_id} (admin)
# ---------------------------------------------------------------------------

class TestDeleteSkill:
    def test_deletes_all_versions(self, client):
        make_skill(client, id="skill-a", version="1.0.0")
        make_skill(client, id="skill-a", version="2.0.0")
        r = client.delete("/skills/skill-a", headers=ADMIN_HEADERS)
        assert r.status_code == 204

    def test_deletes_specific_version(self, client):
        make_skill(client, id="skill-a", version="1.0.0")
        make_skill(client, id="skill-a", version="2.0.0")
        r = client.delete("/skills/skill-a?version=1.0.0", headers=ADMIN_HEADERS)
        assert r.status_code == 204

    def test_not_found_returns_404(self, client):
        r = client.delete("/skills/no-such", headers=ADMIN_HEADERS)
        assert r.status_code == 404

    def test_requires_admin_key(self, client):
        make_skill(client)
        r = client.delete("/skills/skill-a")
        assert r.status_code == 403
