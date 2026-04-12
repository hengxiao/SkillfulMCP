"""
SkillfulLangGraphAgent — a LangChain `Runnable` backed by an explicit
LangGraph `StateGraph` (chatbot ↔ tools), with tools sourced from SkillfulMCP.

Use this instead of `SkillfulLangChainAgent` when you want direct access to
the underlying graph (for custom nodes, checkpointing, or persistence). The
compiled graph is exposed as `agent.graph` after first build.

Example::

    agent = SkillfulLangGraphAgent(
        agent_id="billing-agent",
        server_url="http://localhost:8000",
        admin_key=os.environ["MCP_ADMIN_KEY"],
        llm=ChatAnthropic(model="claude-sonnet-4-6", max_tokens=1024),
        system_prompt="You are a billing specialist.",
    )
    reply: str = agent.run("Look up invoice INV-1234")
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import Runnable, RunnableConfig
from langchain_core.tools import StructuredTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from typing_extensions import TypedDict

from ._base import SkillCallHook, SkillFetcher, default_skill_call


class _AgentState(TypedDict):
    messages: Annotated[list, add_messages]


def _skill_to_tool(skill: dict, on_call: SkillCallHook) -> StructuredTool:
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


def _extra_to_tool(schema: dict, handler: SkillCallHook) -> StructuredTool:
    def _run(**kwargs):
        return json.dumps(handler(schema["name"], kwargs))

    return StructuredTool.from_function(
        func=_run,
        name=schema["name"],
        description=schema["description"],
        args_schema=schema["input_schema"],
    )


class SkillfulLangGraphAgent(Runnable):
    """
    A `Runnable` whose invocation runs through a LangGraph chatbot↔tools loop.
    Tools are fetched from SkillfulMCP on first use.
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
        self._compiled = None  # lazy

    @property
    def agent_id(self) -> str:
        return self._fetcher.agent_id

    def skills(self) -> list[dict]:
        return self._fetcher.skills()

    def bind_extra_tool(self, schema: dict, handler: SkillCallHook) -> None:
        self._extra_tools.append(_extra_to_tool(schema, handler))
        self._compiled = None

    @property
    def graph(self):
        """The compiled LangGraph graph. Built lazily."""
        if self._compiled is None:
            self._build()
        return self._compiled

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------
    def _build(self):
        tools = [_skill_to_tool(s, self._on_skill_call) for s in self.skills()]
        tools.extend(self._extra_tools)
        llm_with_tools = self.llm.bind_tools(tools)
        tool_node = ToolNode(tools)
        system_prompt = self.system_prompt

        def chatbot(state: _AgentState):
            msgs = state["messages"]
            if not msgs or not isinstance(msgs[0], SystemMessage):
                msgs = [SystemMessage(content=system_prompt), *msgs]
            return {"messages": [llm_with_tools.invoke(msgs)]}

        def should_continue(state: _AgentState) -> str:
            last = state["messages"][-1]
            if isinstance(last, AIMessage) and last.tool_calls:
                return "tools"
            return END

        graph = StateGraph(_AgentState)
        graph.add_node("chatbot", chatbot)
        graph.add_node("tools", tool_node)
        graph.add_edge(START, "chatbot")
        graph.add_conditional_edges(
            "chatbot", should_continue, {"tools": "tools", END: END}
        )
        graph.add_edge("tools", "chatbot")
        self._compiled = graph.compile()

    # ------------------------------------------------------------------
    # Runnable interface
    # ------------------------------------------------------------------
    def invoke(self, input: Any, config: RunnableConfig | None = None, **kwargs) -> Any:
        return self.graph.invoke(input, config, **kwargs)

    def run(self, message: str) -> str:
        result = self.invoke({"messages": [HumanMessage(content=message)]})
        for m in reversed(result.get("messages", [])):
            if isinstance(m, AIMessage) and not getattr(m, "tool_calls", None):
                return m.content if isinstance(m.content, str) else str(m.content)
        return ""
