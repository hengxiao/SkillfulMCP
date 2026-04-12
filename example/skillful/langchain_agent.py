"""
SkillfulLangChainAgent — a LangChain `Runnable` that sources its tools from
SkillfulMCP.

Because it subclasses `langchain_core.runnables.Runnable`, instances can drop
into any LangChain chain, be piped with `|`, wrapped in `RunnableParallel`,
etc. The first `.invoke(...)` triggers lazy skill fetching + tool building;
subsequent calls reuse the compiled inner agent graph.

Example::

    agent = SkillfulLangChainAgent(
        agent_id="billing-agent",
        server_url="http://localhost:8000",
        admin_key=os.environ["MCP_ADMIN_KEY"],
        llm=ChatAnthropic(model="claude-sonnet-4-6", max_tokens=1024),
        system_prompt="You are a billing specialist.",
    )
    reply: str = agent.run("Look up invoice INV-1234")
    # or as a Runnable:
    state = agent.invoke({"messages": [HumanMessage(content="…")]})
"""

from __future__ import annotations

import json
from typing import Any

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import Runnable, RunnableConfig
from langchain_core.tools import StructuredTool

from ._base import SkillCallHook, SkillFetcher, default_skill_call


def _skill_to_structured_tool(
    skill: dict, on_call: SkillCallHook
) -> StructuredTool:
    meta = skill.get("metadata") or {}
    schema = meta.get("input_schema") or {"type": "object", "properties": {}}
    tool_name = skill["id"].replace("-", "_")

    def _run(**kwargs):
        return json.dumps(on_call(tool_name, kwargs))

    return StructuredTool.from_function(
        func=_run,
        name=tool_name,
        description=skill.get("description") or skill["name"],
        args_schema=schema,
    )


def _extra_tool_to_structured(schema: dict, handler: SkillCallHook) -> StructuredTool:
    def _run(**kwargs):
        return json.dumps(handler(schema["name"], kwargs))

    return StructuredTool.from_function(
        func=_run,
        name=schema["name"],
        description=schema["description"],
        args_schema=schema["input_schema"],
    )


class SkillfulLangChainAgent(Runnable):
    """
    A LangChain Runnable whose tools are fetched from SkillfulMCP on first use.

    The inner implementation is a compiled LangGraph graph (same object that
    `langchain.agents.create_agent` returns). This subclass extends `Runnable`
    so it composes cleanly with the rest of LangChain.
    """

    def __init__(
        self,
        *,
        agent_id: str,
        server_url: str,
        admin_key: str,
        llm: Any,
        system_prompt: str = "",
        on_skill_call: SkillCallHook | None = None,
    ) -> None:
        self._fetcher = SkillFetcher(agent_id, server_url, admin_key)
        self.llm = llm
        self.system_prompt = system_prompt
        self._on_skill_call = on_skill_call or default_skill_call
        self._extra_tools: list[StructuredTool] = []
        self._compiled = None  # built lazily

    @property
    def agent_id(self) -> str:
        return self._fetcher.agent_id

    def skills(self) -> list[dict]:
        return self._fetcher.skills()

    def bind_extra_tool(self, schema: dict, handler: SkillCallHook) -> None:
        """Register an out-of-catalog tool. Invalidates the compiled graph."""
        self._extra_tools.append(_extra_tool_to_structured(schema, handler))
        self._compiled = None

    # ------------------------------------------------------------------
    # Compile (lazy)
    # ------------------------------------------------------------------
    def _build(self):
        tools = [
            _skill_to_structured_tool(s, self._on_skill_call)
            for s in self.skills()
        ]
        tools.extend(self._extra_tools)
        self._compiled = create_agent(
            model=self.llm, tools=tools, system_prompt=self.system_prompt
        )

    # ------------------------------------------------------------------
    # Runnable interface
    # ------------------------------------------------------------------
    def invoke(self, input: Any, config: RunnableConfig | None = None, **kwargs) -> Any:
        if self._compiled is None:
            self._build()
        return self._compiled.invoke(input, config, **kwargs)

    # Convenience: take a string, return a string (what most users want).
    def run(self, message: str) -> str:
        result = self.invoke({"messages": [HumanMessage(content=message)]})
        for m in reversed(result.get("messages", [])):
            if isinstance(m, AIMessage) and not getattr(m, "tool_calls", None):
                return m.content if isinstance(m.content, str) else str(m.content)
        return ""
