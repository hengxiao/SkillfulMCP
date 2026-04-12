"""Tests for the agent registry service layer (mcp_server.registry)."""

import pytest

from mcp_server.registry import (
    create_agent,
    delete_agent,
    get_agent,
    list_agents,
    update_agent,
)
from mcp_server.schemas import AgentCreate, AgentUpdate


def _create(db, **kwargs):
    defaults = dict(
        id="agent-1",
        name="Test Agent",
        skillsets=["ss-1"],
        skills=["skill-a"],
        scope=["read"],
    )
    defaults.update(kwargs)
    return create_agent(db, AgentCreate(**defaults))


class TestCreateAgent:
    def test_creates_agent(self, db_session):
        agent = _create(db_session)
        assert agent.id == "agent-1"
        assert agent.name == "Test Agent"
        assert agent.skillsets == ["ss-1"]
        assert agent.scope == ["read"]

    def test_duplicate_id_raises_value_error(self, db_session):
        _create(db_session)
        with pytest.raises(ValueError, match="already exists"):
            _create(db_session)

    def test_multiple_scopes(self, db_session):
        agent = _create(db_session, scope=["read", "execute"])
        assert set(agent.scope) == {"read", "execute"}

    def test_empty_skillsets(self, db_session):
        agent = _create(db_session, skillsets=[], skills=[])
        assert agent.skillsets == []
        assert agent.skills == []


class TestGetAgent:
    def test_get_existing(self, db_session):
        _create(db_session)
        agent = get_agent(db_session, "agent-1")
        assert agent is not None
        assert agent.id == "agent-1"

    def test_get_nonexistent_returns_none(self, db_session):
        assert get_agent(db_session, "no-such-agent") is None


class TestListAgents:
    def test_empty_db(self, db_session):
        assert list_agents(db_session) == []

    def test_multiple_agents(self, db_session):
        _create(db_session, id="agent-1")
        _create(db_session, id="agent-2")
        agents = list_agents(db_session)
        assert len(agents) == 2
        assert {a.id for a in agents} == {"agent-1", "agent-2"}


class TestUpdateAgent:
    def test_update_name(self, db_session):
        _create(db_session)
        updated = update_agent(db_session, "agent-1", AgentUpdate(name="New Name"))
        assert updated.name == "New Name"
        # Other fields unchanged
        assert updated.skillsets == ["ss-1"]

    def test_update_skillsets(self, db_session):
        _create(db_session)
        updated = update_agent(db_session, "agent-1", AgentUpdate(skillsets=["ss-2", "ss-3"]))
        assert set(updated.skillsets) == {"ss-2", "ss-3"}

    def test_update_scope(self, db_session):
        _create(db_session, scope=["read"])
        updated = update_agent(db_session, "agent-1", AgentUpdate(scope=["read", "execute"]))
        assert set(updated.scope) == {"read", "execute"}

    def test_update_nonexistent_returns_none(self, db_session):
        result = update_agent(db_session, "no-agent", AgentUpdate(name="X"))
        assert result is None

    def test_partial_update_preserves_other_fields(self, db_session):
        _create(db_session, skills=["s1", "s2"])
        updated = update_agent(db_session, "agent-1", AgentUpdate(name="Renamed"))
        assert updated.skills == ["s1", "s2"]


class TestDeleteAgent:
    def test_delete_existing(self, db_session):
        _create(db_session)
        found = delete_agent(db_session, "agent-1")
        assert found is True
        assert get_agent(db_session, "agent-1") is None

    def test_delete_nonexistent_returns_false(self, db_session):
        assert delete_agent(db_session, "no-agent") is False
