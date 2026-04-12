"""
Unit tests for the reusable Skillful* agent classes.

The tests stub out the catalog calls (`get_agent_token`, `load_agent_skills`)
with monkeypatching so they run without a live MCP server, and verify:

- lazy token + skill fetching (only on first need),
- correct translation of MCP skill metadata into each framework's tool format,
- `bind_extra_tool` wiring for the orchestrator's `route_to_agent` pattern,
- that LangChain and LangGraph classes are `Runnable` subclasses.
"""

from __future__ import annotations

import pytest
from langchain_core.runnables import Runnable

from example.skillful import (
    SkillfulAnthropicAgent,
    SkillfulLangChainAgent,
    SkillfulLangGraphAgent,
    SkillfulOpenAIAgent,
)
from example.skillful import _base as skillful_base


SAMPLE_SKILLS = [
    {
        "id": "lookup-invoice",
        "name": "Lookup Invoice",
        "description": "Finds an invoice.",
        "metadata": {
            "input_schema": {
                "type": "object",
                "properties": {"invoice_id": {"type": "string"}},
                "required": ["invoice_id"],
            },
        },
    },
    {
        "id": "apply-credit",
        "name": "Apply Credit",
        "description": "Applies a credit.",
        "metadata": {},  # no input_schema — should default to empty object
    },
]


@pytest.fixture(autouse=True)
def stub_catalog(monkeypatch):
    """Make every Skillful* agent think the catalog already authorized SAMPLE_SKILLS."""
    monkeypatch.setattr(skillful_base, "get_agent_token", lambda *_a, **_k: "test-jwt")
    monkeypatch.setattr(
        skillful_base, "load_agent_skills", lambda *_a, **_k: SAMPLE_SKILLS
    )


# ---------------------------------------------------------------------------
# SkillfulAnthropicAgent
# ---------------------------------------------------------------------------

class TestAnthropicAgent:
    def _agent(self):
        # Pass an object for `client` — we never actually call it in these tests.
        return SkillfulAnthropicAgent(
            agent_id="billing-agent",
            server_url="http://localhost:8000",
            admin_key="admin",
            client=object(),
            system_prompt="you are billing",
        )

    def test_tools_translation(self):
        tools = self._agent().tools()
        assert [t["name"] for t in tools] == ["lookup_invoice", "apply_credit"]
        # Schema passthrough.
        assert tools[0]["input_schema"]["required"] == ["invoice_id"]
        # Missing input_schema becomes empty object.
        assert tools[1]["input_schema"] == {"type": "object", "properties": {}}

    def test_bind_extra_tool_appends(self):
        agent = self._agent()
        schema = {
            "name": "route_to_agent",
            "description": "route",
            "input_schema": {"type": "object", "properties": {}},
        }
        agent.bind_extra_tool(schema, lambda n, a: {"ok": True})
        tool_names = [t["name"] for t in agent.tools()]
        assert "route_to_agent" in tool_names

    def test_lazy_token_fetch(self, monkeypatch):
        calls = {"n": 0}
        def spy(*_a, **_k):
            calls["n"] += 1
            return "fake-token"
        monkeypatch.setattr(skillful_base, "get_agent_token", spy)
        agent = self._agent()
        assert calls["n"] == 0  # ctor must not fetch
        _ = agent._fetcher.token
        _ = agent._fetcher.token
        assert calls["n"] == 1  # cached after first fetch


# ---------------------------------------------------------------------------
# SkillfulOpenAIAgent
# ---------------------------------------------------------------------------

class TestOpenAIAgent:
    def _agent(self):
        return SkillfulOpenAIAgent(
            agent_id="billing-agent",
            server_url="http://localhost:8000",
            admin_key="admin",
            client=object(),
        )

    def test_tools_translation(self):
        tools = self._agent().tools()
        # OpenAI wraps everything in {type: "function", function: {...}}.
        assert all(t["type"] == "function" for t in tools)
        names = [t["function"]["name"] for t in tools]
        assert names == ["lookup_invoice", "apply_credit"]
        # Schema lands under function.parameters.
        assert tools[0]["function"]["parameters"]["required"] == ["invoice_id"]

    def test_bind_extra_tool_converts_to_openai_shape(self):
        agent = self._agent()
        agent.bind_extra_tool(
            {"name": "route", "description": "r", "input_schema": {"type": "object", "properties": {}}},
            lambda n, a: {"ok": True},
        )
        last = agent.tools()[-1]
        assert last["type"] == "function"
        assert last["function"]["name"] == "route"
        assert last["function"]["parameters"] == {"type": "object", "properties": {}}


# ---------------------------------------------------------------------------
# SkillfulLangChainAgent
# ---------------------------------------------------------------------------

class TestLangChainAgent:
    def test_is_runnable_subclass(self):
        assert issubclass(SkillfulLangChainAgent, Runnable)

    def test_lazy_compile(self, monkeypatch):
        """The inner graph must not be built until invoke is called."""
        # Patch create_agent so we can count calls without a real LLM.
        import example.skillful.langchain_agent as mod

        calls = {"n": 0}
        def fake_create_agent(**kwargs):
            calls["n"] += 1
            return object()  # placeholder; we won't invoke it
        monkeypatch.setattr(mod, "create_agent", fake_create_agent)

        agent = SkillfulLangChainAgent(
            agent_id="billing-agent",
            server_url="http://localhost:8000",
            admin_key="admin",
            llm=object(),
        )
        assert calls["n"] == 0  # ctor does nothing
        agent._build()
        assert calls["n"] == 1

    def test_bind_extra_tool_invalidates_build(self, monkeypatch):
        import example.skillful.langchain_agent as mod
        monkeypatch.setattr(mod, "create_agent", lambda **kw: object())
        agent = SkillfulLangChainAgent(
            agent_id="billing-agent",
            server_url="http://localhost:8000",
            admin_key="admin",
            llm=object(),
        )
        agent._build()
        assert agent._compiled is not None
        agent.bind_extra_tool(
            {"name": "route", "description": "r", "input_schema": {"type": "object", "properties": {}}},
            lambda n, a: {"ok": True},
        )
        assert agent._compiled is None  # invalidated


# ---------------------------------------------------------------------------
# SkillfulLangGraphAgent
# ---------------------------------------------------------------------------

class TestLangGraphAgent:
    def test_is_runnable_subclass(self):
        assert issubclass(SkillfulLangGraphAgent, Runnable)

    def test_graph_property_builds_on_access(self, monkeypatch):
        """Accessing .graph should build the compiled graph once."""
        class FakeLLM:
            def bind_tools(self, tools):
                return self
            def invoke(self, msgs):
                return None

        agent = SkillfulLangGraphAgent(
            agent_id="billing-agent",
            server_url="http://localhost:8000",
            admin_key="admin",
            llm=FakeLLM(),
        )
        assert agent._compiled is None
        g = agent.graph
        assert agent._compiled is g  # cached


# ---------------------------------------------------------------------------
# Common behavior — dispatcher override
# ---------------------------------------------------------------------------

def test_on_skill_call_override_is_used():
    calls = []
    def handler(name, args):
        calls.append((name, args))
        return {"from": "custom"}

    agent = SkillfulAnthropicAgent(
        agent_id="billing-agent",
        server_url="http://localhost:8000",
        admin_key="admin",
        client=object(),
        on_skill_call=handler,
    )
    result = agent._dispatch("lookup_invoice", {"invoice_id": "INV-1"})
    assert result == {"from": "custom"}
    assert calls == [("lookup_invoice", {"invoice_id": "INV-1"})]
