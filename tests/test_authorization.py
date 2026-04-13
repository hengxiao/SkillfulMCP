"""Tests for the authorization engine (mcp_server.authorization)."""


from mcp_server.authorization import resolve_allowed_skill_ids
from mcp_server.catalog import (
    add_skill_to_skillset,
    create_skill,
    create_skillset,
)
from mcp_server.schemas import SkillCreate, SkillsetCreate


def _ss(db, id="ss-1"):
    return create_skillset(db, SkillsetCreate(id=id, name=f"Skillset {id}"))


def _skill(db, id="skill-a", version="1.0.0"):
    return create_skill(
        db,
        SkillCreate(id=id, name=f"Skill {id}", version=version, metadata={}, skillset_ids=[]),
    )


class TestResolveAllowedSkillIds:
    def test_explicit_skills_in_claims(self, db_session):
        _skill(db_session, "skill-a")
        _skill(db_session, "skill-b")
        claims = {"skills": ["skill-a"], "skillsets": []}
        allowed = resolve_allowed_skill_ids(claims, db_session)
        assert "skill-a" in allowed
        assert "skill-b" not in allowed

    def test_skillset_grants_its_members(self, db_session):
        _ss(db_session, "ss-1")
        _skill(db_session, "skill-a")
        _skill(db_session, "skill-b")
        add_skill_to_skillset(db_session, "ss-1", "skill-a")
        add_skill_to_skillset(db_session, "ss-1", "skill-b")

        claims = {"skills": [], "skillsets": ["ss-1"]}
        allowed = resolve_allowed_skill_ids(claims, db_session)
        assert {"skill-a", "skill-b"}.issubset(allowed)

    def test_union_of_both_paths(self, db_session):
        _ss(db_session, "ss-1")
        _skill(db_session, "skill-a")
        _skill(db_session, "skill-b")
        _skill(db_session, "skill-c")
        add_skill_to_skillset(db_session, "ss-1", "skill-a")

        claims = {"skills": ["skill-b", "skill-c"], "skillsets": ["ss-1"]}
        allowed = resolve_allowed_skill_ids(claims, db_session)
        assert {"skill-a", "skill-b", "skill-c"}.issubset(allowed)

    def test_empty_claims_returns_empty_set(self, db_session):
        claims = {"skills": [], "skillsets": []}
        assert resolve_allowed_skill_ids(claims, db_session) == set()

    def test_missing_keys_defaults_to_empty(self, db_session):
        allowed = resolve_allowed_skill_ids({}, db_session)
        assert allowed == set()

    def test_unknown_skillset_returns_empty(self, db_session):
        claims = {"skills": [], "skillsets": ["nonexistent-ss"]}
        allowed = resolve_allowed_skill_ids(claims, db_session)
        assert allowed == set()

    def test_overlapping_grants_are_deduplicated(self, db_session):
        _ss(db_session, "ss-1")
        _skill(db_session, "skill-a")
        add_skill_to_skillset(db_session, "ss-1", "skill-a")

        claims = {"skills": ["skill-a"], "skillsets": ["ss-1"]}
        allowed = resolve_allowed_skill_ids(claims, db_session)
        # skill-a appears in both paths but must appear only once
        assert allowed == {"skill-a"}

    def test_multiple_skillsets(self, db_session):
        _ss(db_session, "ss-1")
        _ss(db_session, "ss-2")
        _skill(db_session, "skill-a")
        _skill(db_session, "skill-b")
        add_skill_to_skillset(db_session, "ss-1", "skill-a")
        add_skill_to_skillset(db_session, "ss-2", "skill-b")

        claims = {"skills": [], "skillsets": ["ss-1", "ss-2"]}
        allowed = resolve_allowed_skill_ids(claims, db_session)
        assert {"skill-a", "skill-b"}.issubset(allowed)
