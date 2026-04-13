"""
Wave 8a tests — public/private visibility on skills + skillsets.

Covers the model + schema + authorization rule update. The Web UI badge
+ form-radio rendering is covered by the existing webui smoke + e2e
tests (which now include visibility in the rendered HTML); these tests
focus on the auth-model-changing path.
"""

from __future__ import annotations

import pytest

from mcp_server.authorization import resolve_allowed_skill_ids
from mcp_server.catalog import (
    add_skill_to_skillset,
    create_skill,
    create_skillset,
)
from mcp_server.schemas import SkillCreate, SkillsetCreate

from tests.conftest import (
    ADMIN_HEADERS,
    bearer,
    get_token,
    make_agent,
    make_skill,
    make_skillset,
)


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

class TestSchemaValidation:
    def test_default_is_private(self):
        s = SkillCreate(id="s", name="S", version="1.0.0")
        assert s.visibility == "private"
        ss = SkillsetCreate(id="x", name="X")
        assert ss.visibility == "private"

    def test_public_accepted(self):
        SkillCreate(id="s", name="S", version="1.0.0", visibility="public")
        SkillsetCreate(id="x", name="X", visibility="public")

    def test_unknown_value_rejected(self):
        with pytest.raises(ValueError, match="visibility"):
            SkillCreate(id="s", name="S", version="1.0.0", visibility="restricted")
        with pytest.raises(ValueError, match="visibility"):
            SkillsetCreate(id="x", name="X", visibility="restricted")


# ---------------------------------------------------------------------------
# Service layer — visibility persisted
# ---------------------------------------------------------------------------

class TestServicePersistence:
    def test_skill_visibility_default_and_explicit(self, db_session):
        s_default = create_skill(
            db_session,
            SkillCreate(id="a", name="A", version="1.0.0"),
        )
        assert s_default.visibility == "private"
        s_public = create_skill(
            db_session,
            SkillCreate(id="b", name="B", version="1.0.0", visibility="public"),
        )
        assert s_public.visibility == "public"

    def test_skillset_visibility(self, db_session):
        ss = create_skillset(
            db_session,
            SkillsetCreate(id="x", name="X", visibility="public"),
        )
        assert ss.visibility == "public"


# ---------------------------------------------------------------------------
# Authorization rule — public skills/skillsets bypass grant requirement
# ---------------------------------------------------------------------------

class TestAuthorizationRule:
    def test_private_skill_requires_grant(self, db_session):
        create_skill(db_session, SkillCreate(id="private-s", name="P", version="1.0.0"))
        # No grants, no skillsets — agent gets nothing.
        allowed = resolve_allowed_skill_ids({"skills": [], "skillsets": []}, db_session)
        assert "private-s" not in allowed

    def test_public_skill_visible_without_grant(self, db_session):
        create_skill(
            db_session,
            SkillCreate(id="public-s", name="P", version="1.0.0", visibility="public"),
        )
        allowed = resolve_allowed_skill_ids({"skills": [], "skillsets": []}, db_session)
        assert "public-s" in allowed

    def test_public_skillset_exposes_all_members(self, db_session):
        create_skillset(
            db_session,
            SkillsetCreate(id="ss-public", name="SS", visibility="public"),
        )
        # Both members are PRIVATE skills — yet the public skillset exposes them.
        create_skill(db_session, SkillCreate(id="m1", name="M1", version="1.0.0"))
        create_skill(db_session, SkillCreate(id="m2", name="M2", version="1.0.0"))
        add_skill_to_skillset(db_session, "ss-public", "m1")
        add_skill_to_skillset(db_session, "ss-public", "m2")

        # Agent with no grants still sees both via the public skillset.
        allowed = resolve_allowed_skill_ids({"skills": [], "skillsets": []}, db_session)
        assert {"m1", "m2"} <= allowed

    def test_private_skillset_does_not_leak(self, db_session):
        create_skillset(db_session, SkillsetCreate(id="ss-private", name="SS"))
        create_skill(db_session, SkillCreate(id="hidden", name="H", version="1.0.0"))
        add_skill_to_skillset(db_session, "ss-private", "hidden")
        allowed = resolve_allowed_skill_ids({"skills": [], "skillsets": []}, db_session)
        assert "hidden" not in allowed

    def test_explicit_grant_still_works(self, db_session):
        """The pre-Wave-8 behavior must keep working — a skill in claims.skills
        is reachable regardless of visibility."""
        create_skill(db_session, SkillCreate(id="x", name="X", version="1.0.0"))
        allowed = resolve_allowed_skill_ids({"skills": ["x"], "skillsets": []}, db_session)
        assert "x" in allowed


# ---------------------------------------------------------------------------
# HTTP integration — public skill is visible to a barebones agent
# ---------------------------------------------------------------------------

class TestHTTPIntegration:
    def test_public_skill_in_GET_skills_for_unprivileged_agent(self, client):
        # Create one private + one public skill.
        client.post(
            "/skills",
            json={"id": "secret", "name": "S", "version": "1.0.0", "metadata": {}},
            headers=ADMIN_HEADERS,
        ).raise_for_status()
        client.post(
            "/skills",
            json={"id": "shared", "name": "S", "version": "1.0.0",
                  "metadata": {}, "visibility": "public"},
            headers=ADMIN_HEADERS,
        ).raise_for_status()

        # Agent with NO skill or skillset grants.
        make_agent(client, id="bare-agent", scope=["read"])
        token = get_token(client, "bare-agent")

        r = client.get("/skills", headers=bearer(token))
        assert r.status_code == 200
        ids = sorted(s["id"] for s in r.json())
        assert "shared" in ids
        assert "secret" not in ids

    def test_public_skill_metadata_carries_visibility(self, client):
        client.post(
            "/skills",
            json={"id": "shared", "name": "S", "version": "1.0.0",
                  "metadata": {}, "visibility": "public"},
            headers=ADMIN_HEADERS,
        ).raise_for_status()
        r = client.get("/admin/skills/shared", headers=ADMIN_HEADERS)
        assert r.status_code == 200
        assert r.json()["visibility"] == "public"

    def test_visibility_persists_through_PUT_upsert(self, client):
        # Initial: private
        client.post(
            "/skills",
            json={"id": "x", "name": "X", "version": "1.0.0", "metadata": {}},
            headers=ADMIN_HEADERS,
        ).raise_for_status()
        # Upsert to public
        client.put(
            "/skills/x",
            json={"name": "X", "version": "1.0.0", "metadata": {},
                  "visibility": "public"},
            headers=ADMIN_HEADERS,
        ).raise_for_status()
        r = client.get("/admin/skills/x", headers=ADMIN_HEADERS)
        assert r.json()["visibility"] == "public"

    def test_invalid_visibility_returns_422(self, client):
        r = client.post(
            "/skills",
            json={"id": "x", "name": "X", "version": "1.0.0",
                  "metadata": {}, "visibility": "secret"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 422
