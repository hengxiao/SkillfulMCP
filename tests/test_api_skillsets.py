"""Integration tests for the /skillsets API endpoints."""

from tests.conftest import (
    ADMIN_HEADERS,
    bearer,
    get_token,
    make_agent,
    make_skill,
    make_skillset,
)


def _auth_headers(client, *, skillsets=None, skills=None):
    make_agent(
        client,
        id="ss-test-agent",
        skillsets=skillsets or [],
        skills=skills or [],
    )
    token = get_token(client, "ss-test-agent")
    return bearer(token)


# ---------------------------------------------------------------------------
# GET /skillsets (admin)
# ---------------------------------------------------------------------------

class TestListSkillsets:
    def test_empty_initially(self, client):
        r = client.get("/skillsets", headers=ADMIN_HEADERS)
        assert r.status_code == 200
        assert r.json() == []

    def test_lists_created_skillsets(self, client):
        make_skillset(client, id="ss-1")
        make_skillset(client, id="ss-2")
        r = client.get("/skillsets", headers=ADMIN_HEADERS)
        ids = [ss["id"] for ss in r.json()]
        assert {"ss-1", "ss-2"}.issubset(set(ids))

    def test_requires_admin_key(self, client):
        r = client.get("/skillsets")
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# POST /skillsets (admin)
# ---------------------------------------------------------------------------

class TestCreateSkillset:
    def test_creates_skillset(self, client):
        r = client.post(
            "/skillsets",
            json={"id": "ss-new", "name": "New Skillset"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 201
        assert r.json()["id"] == "ss-new"

    def test_duplicate_returns_409(self, client):
        make_skillset(client)
        r = client.post(
            "/skillsets",
            json={"id": "test-ss", "name": "Dup"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 409

    def test_requires_admin_key(self, client):
        r = client.post("/skillsets", json={"id": "x", "name": "X"})
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# PUT /skillsets/{id} (admin)
# ---------------------------------------------------------------------------

class TestUpsertSkillset:
    def test_creates_if_absent(self, client):
        r = client.put(
            "/skillsets/ss-new",
            json={"id": "ss-new", "name": "Created via PUT"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 200
        assert r.json()["name"] == "Created via PUT"

    def test_updates_existing(self, client):
        make_skillset(client, id="ss-1")
        r = client.put(
            "/skillsets/ss-1",
            json={"id": "ss-1", "name": "Updated Name"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 200
        assert r.json()["name"] == "Updated Name"


# ---------------------------------------------------------------------------
# DELETE /skillsets/{id} (admin)
# ---------------------------------------------------------------------------

class TestDeleteSkillset:
    def test_deletes_skillset(self, client):
        make_skillset(client, id="ss-1")
        r = client.delete("/skillsets/ss-1", headers=ADMIN_HEADERS)
        assert r.status_code == 204

    def test_not_found_returns_404(self, client):
        r = client.delete("/skillsets/ghost", headers=ADMIN_HEADERS)
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# GET /skillsets/{id}/skills (agent JWT)
# ---------------------------------------------------------------------------

class TestListSkillsetSkills:
    def test_returns_authorized_skills_only(self, client):
        make_skillset(client, id="ss-1")
        make_skill(client, id="skill-a", skillset_ids=["ss-1"])
        make_skill(client, id="skill-b", skillset_ids=["ss-1"])

        # Agent only has skill-a
        headers = _auth_headers(client, skills=["skill-a"])
        r = client.get("/skillsets/ss-1/skills", headers=headers)
        assert r.status_code == 200
        ids = [s["id"] for s in r.json()]
        assert "skill-a" in ids
        assert "skill-b" not in ids

    def test_returns_empty_for_unathorized_agent(self, client):
        make_skillset(client, id="ss-1")
        make_skill(client, id="skill-a", skillset_ids=["ss-1"])

        headers = _auth_headers(client)  # no skills
        r = client.get("/skillsets/ss-1/skills", headers=headers)
        assert r.status_code == 200
        assert r.json() == []

    def test_requires_bearer_token(self, client):
        r = client.get("/skillsets/ss-1/skills")
        assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# PUT /skillsets/{id}/skills/{skill_id} (admin)
# ---------------------------------------------------------------------------

class TestAssociateSkill:
    def test_associates_skill(self, client):
        make_skillset(client, id="ss-1")
        make_skill(client, id="skill-a")
        r = client.put("/skillsets/ss-1/skills/skill-a", headers=ADMIN_HEADERS)
        assert r.status_code == 204

    def test_idempotent(self, client):
        make_skillset(client, id="ss-1")
        make_skill(client, id="skill-a")
        client.put("/skillsets/ss-1/skills/skill-a", headers=ADMIN_HEADERS)
        r = client.put("/skillsets/ss-1/skills/skill-a", headers=ADMIN_HEADERS)
        assert r.status_code == 204

    def test_unknown_skillset_returns_404(self, client):
        make_skill(client, id="skill-a")
        r = client.put("/skillsets/no-ss/skills/skill-a", headers=ADMIN_HEADERS)
        assert r.status_code == 404

    def test_unknown_skill_returns_404(self, client):
        make_skillset(client, id="ss-1")
        r = client.put("/skillsets/ss-1/skills/no-skill", headers=ADMIN_HEADERS)
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /skillsets/{id}/skills/{skill_id} (admin)
# ---------------------------------------------------------------------------

class TestDisassociateSkill:
    def test_removes_association(self, client):
        make_skillset(client, id="ss-1")
        make_skill(client, id="skill-a", skillset_ids=["ss-1"])
        r = client.delete("/skillsets/ss-1/skills/skill-a", headers=ADMIN_HEADERS)
        assert r.status_code == 204

    def test_not_found_returns_404(self, client):
        make_skillset(client, id="ss-1")
        r = client.delete("/skillsets/ss-1/skills/ghost", headers=ADMIN_HEADERS)
        assert r.status_code == 404
