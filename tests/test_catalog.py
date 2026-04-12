"""Tests for the skill catalog service layer (mcp_server.catalog)."""

import pytest

from mcp_server.catalog import (
    add_skill_to_skillset,
    create_skill,
    create_skillset,
    delete_skill_all,
    delete_skill_version,
    get_skill_latest,
    get_skill_version,
    get_skill_versions,
    list_skills_for_agent,
    list_skills_in_skillset,
    remove_skill_from_skillset,
    upsert_skill,
    upsert_skillset,
)
from mcp_server.schemas import SkillCreate, SkillsetCreate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ss(db, id="ss-1", name="Skillset One"):
    return create_skillset(db, SkillsetCreate(id=id, name=name))


def _skill(db, *, id="skill-a", version="1.0.0", skillset_ids=None):
    return create_skill(
        db,
        SkillCreate(
            id=id,
            name=f"Skill {id}",
            description="desc",
            version=version,
            metadata={"k": "v"},
            skillset_ids=skillset_ids or [],
        ),
    )


# ---------------------------------------------------------------------------
# Skillset CRUD
# ---------------------------------------------------------------------------

class TestSkillsetCRUD:
    def test_create_and_retrieve(self, db_session):
        ss = _ss(db_session)
        assert ss.id == "ss-1"
        assert ss.name == "Skillset One"

    def test_duplicate_raises_value_error(self, db_session):
        _ss(db_session)
        with pytest.raises(ValueError, match="already exists"):
            _ss(db_session)

    def test_upsert_updates_existing(self, db_session):
        _ss(db_session)
        updated = upsert_skillset(
            db_session, "ss-1", SkillsetCreate(id="ss-1", name="Updated Name")
        )
        assert updated.name == "Updated Name"

    def test_upsert_creates_if_absent(self, db_session):
        ss = upsert_skillset(
            db_session, "ss-new", SkillsetCreate(id="ss-new", name="New SS")
        )
        assert ss.id == "ss-new"


# ---------------------------------------------------------------------------
# Skill CRUD and versioning
# ---------------------------------------------------------------------------

class TestSkillCreate:
    def test_creates_with_is_latest_true(self, db_session):
        skill = _skill(db_session)
        assert skill.is_latest is True
        assert skill.version == "1.0.0"

    def test_duplicate_version_raises_value_error(self, db_session):
        _skill(db_session)
        with pytest.raises(ValueError, match="already exists"):
            _skill(db_session)

    def test_invalid_skillset_raises_value_error(self, db_session):
        with pytest.raises(ValueError, match="does not exist"):
            _skill(db_session, skillset_ids=["nonexistent-ss"])

    def test_skillset_association_created(self, db_session):
        _ss(db_session)
        skill = _skill(db_session, skillset_ids=["ss-1"])
        skills_in_ss = list_skills_in_skillset(db_session, "ss-1")
        assert any(s.id == skill.id for s in skills_in_ss)


class TestSkillVersioning:
    def test_is_latest_updated_on_new_version(self, db_session):
        v1 = _skill(db_session, version="1.0.0")
        assert v1.is_latest is True
        v2 = _skill(db_session, version="2.0.0")
        assert v2.is_latest is True
        # Reload v1
        db_session.refresh(v1)
        assert v1.is_latest is False

    def test_is_latest_uses_semver_not_insertion_order(self, db_session):
        """Insert 2.0.0 before 1.5.0; 2.0.0 must still be latest."""
        v2 = _skill(db_session, version="2.0.0")
        v15 = _skill(db_session, version="1.5.0")
        db_session.refresh(v2)
        assert v2.is_latest is True
        assert v15.is_latest is False

    def test_get_skill_latest(self, db_session):
        _skill(db_session, version="1.0.0")
        _skill(db_session, version="1.2.0")
        latest = get_skill_latest(db_session, "skill-a")
        assert latest.version == "1.2.0"

    def test_get_skill_version(self, db_session):
        _skill(db_session, version="1.0.0")
        _skill(db_session, version="2.0.0")
        v1 = get_skill_version(db_session, "skill-a", "1.0.0")
        assert v1 is not None
        assert v1.version == "1.0.0"

    def test_get_skill_versions_sorted(self, db_session):
        for v in ("2.0.0", "1.0.0", "1.5.0"):
            _skill(db_session, version=v)
        versions = [s.version for s in get_skill_versions(db_session, "skill-a")]
        assert versions == ["1.0.0", "1.5.0", "2.0.0"]

    def test_get_latest_returns_none_for_unknown(self, db_session):
        assert get_skill_latest(db_session, "no-such-skill") is None


class TestSkillUpsert:
    def test_upsert_creates_new(self, db_session):
        skill = upsert_skill(db_session, "skill-x", "Skill X", "desc", "1.0.0", {})
        assert skill.id == "skill-x"
        assert skill.is_latest is True

    def test_upsert_updates_existing(self, db_session):
        _skill(db_session, id="skill-a", version="1.0.0")
        updated = upsert_skill(db_session, "skill-a", "Updated Name", "new desc", "1.0.0", {"new": "meta"})
        assert updated.name == "Updated Name"
        assert updated.description == "new desc"

    def test_upsert_new_version_updates_latest(self, db_session):
        _skill(db_session, version="1.0.0")
        upsert_skill(db_session, "skill-a", "Skill a", "d", "2.0.0", {})
        latest = get_skill_latest(db_session, "skill-a")
        assert latest.version == "2.0.0"


class TestSkillDelete:
    def test_delete_all_versions(self, db_session):
        _skill(db_session, version="1.0.0")
        _skill(db_session, version="2.0.0")
        n = delete_skill_all(db_session, "skill-a")
        assert n == 2
        assert get_skill_latest(db_session, "skill-a") is None

    def test_delete_specific_version(self, db_session):
        _skill(db_session, version="1.0.0")
        _skill(db_session, version="2.0.0")
        found = delete_skill_version(db_session, "skill-a", "1.0.0")
        assert found is True
        remaining = get_skill_versions(db_session, "skill-a")
        assert len(remaining) == 1
        assert remaining[0].version == "2.0.0"

    def test_delete_latest_promotes_next(self, db_session):
        _skill(db_session, version="1.0.0")
        _skill(db_session, version="2.0.0")
        delete_skill_version(db_session, "skill-a", "2.0.0")
        latest = get_skill_latest(db_session, "skill-a")
        assert latest.version == "1.0.0"
        assert latest.is_latest is True

    def test_delete_missing_version_returns_false(self, db_session):
        assert delete_skill_version(db_session, "skill-a", "9.9.9") is False

    def test_delete_missing_skill_returns_zero(self, db_session):
        assert delete_skill_all(db_session, "no-such") == 0


# ---------------------------------------------------------------------------
# Skillset membership
# ---------------------------------------------------------------------------

class TestSkillsetMembership:
    def test_add_and_list(self, db_session):
        _ss(db_session)
        _skill(db_session)
        add_skill_to_skillset(db_session, "ss-1", "skill-a")
        skills = list_skills_in_skillset(db_session, "ss-1")
        assert any(s.id == "skill-a" for s in skills)

    def test_add_nonexistent_skillset_raises(self, db_session):
        _skill(db_session)
        with pytest.raises(ValueError, match="does not exist"):
            add_skill_to_skillset(db_session, "no-ss", "skill-a")

    def test_add_nonexistent_skill_raises(self, db_session):
        _ss(db_session)
        with pytest.raises(ValueError, match="does not exist"):
            add_skill_to_skillset(db_session, "ss-1", "no-skill")

    def test_add_is_idempotent(self, db_session):
        _ss(db_session)
        _skill(db_session)
        add_skill_to_skillset(db_session, "ss-1", "skill-a")
        add_skill_to_skillset(db_session, "ss-1", "skill-a")
        skills = list_skills_in_skillset(db_session, "ss-1")
        assert sum(1 for s in skills if s.id == "skill-a") == 1

    def test_remove_association(self, db_session):
        _ss(db_session)
        _skill(db_session)
        add_skill_to_skillset(db_session, "ss-1", "skill-a")
        found = remove_skill_from_skillset(db_session, "ss-1", "skill-a")
        assert found is True
        assert list_skills_in_skillset(db_session, "ss-1") == []

    def test_remove_missing_returns_false(self, db_session):
        _ss(db_session)
        assert remove_skill_from_skillset(db_session, "ss-1", "skill-a") is False

    def test_delete_skillset_cascades_to_links(self, db_session):
        from mcp_server.catalog import delete_skillset
        _ss(db_session)
        _skill(db_session)
        add_skill_to_skillset(db_session, "ss-1", "skill-a")
        delete_skillset(db_session, "ss-1")
        # Skill itself must still exist
        assert get_skill_latest(db_session, "skill-a") is not None

    def test_list_skills_for_agent(self, db_session):
        _skill(db_session, id="skill-a", version="1.0.0")
        _skill(db_session, id="skill-b", version="1.0.0")
        _skill(db_session, id="skill-c", version="1.0.0")
        skills = list_skills_for_agent(db_session, {"skill-a", "skill-c"})
        ids = {s.id for s in skills}
        assert ids == {"skill-a", "skill-c"}

    def test_list_skills_for_agent_empty_set(self, db_session):
        assert list_skills_for_agent(db_session, set()) == []
