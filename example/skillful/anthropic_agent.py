"""
SkillfulAnthropicAgent — a reusable Claude-backed agent that sources its tools
from SkillfulMCP at run time.

Example::

    agent = SkillfulAnthropicAgent(
        agent_id="billing-agent",
        server_url="http://localhost:8000",
        admin_key=os.environ["MCP_ADMIN_KEY"],
        client=anthropic.Anthropic(),
        model="claude-sonnet-4-6",
        system_prompt="You are a billing specialist.",
    )
    reply = agent.run("Look up invoice INV-1234")
"""

from __future__ import annotations

import json
from typing import Any

import anthropic

from ._base import SkillCallHook, SkillFetcher, default_skill_call


def _skills_to_anthropic_tools(skills: list[dict]) -> list[dict]:
    tools = []
    for skill in skills:
        meta = skill.get("metadata") or {}
        tools.append({
            "name": skill["id"].replace("-", "_"),
            "description": skill.get("description") or skill["name"],
            "input_schema": meta.get("input_schema") or {
                "type": "object",
                "properties": {},
            },
        })
    return tools


class SkillfulAnthropicAgent:
    """Claude-backed agent with tools sourced from SkillfulMCP.

    The Anthropic SDK has no public agent base class, so this is a plain
    class that wraps `client.messages.create` in the standard tool-use loop.
    """

    def __init__(
        self,
        *,
        agent_id: str,
        server_url: str,
        admin_key: str,
        client: anthropic.Anthropic | None = None,
        model: str = "claude-sonnet-4-6",
        system_prompt: str = "",
        max_tokens: int = 1024,
        max_steps: int = 10,
        on_skill_call: SkillCallHook | None = None,
    ) -> None:
        self._fetcher = SkillFetcher(agent_id, server_url, admin_key)
        self.client = client or anthropic.Anthropic()
        self.model = model
        self.system_prompt = system_prompt
        self.max_tokens = max_tokens
        self.max_steps = max_steps
        self._on_skill_call = on_skill_call or default_skill_call
        self._extra_tools: list[dict] = []
        self._extra_tool_handlers: dict[str, SkillCallHook] = {}

    # ------------------------------------------------------------------
    # Identity / introspection
    # ------------------------------------------------------------------
    @property
    def agent_id(self) -> str:
        return self._fetcher.agent_id

    def skills(self) -> list[dict]:
        """MCP skill records authorized for this agent (cached)."""
        return self._fetcher.skills()

    def tools(self) -> list[dict]:
        """Anthropic tool definitions (catalog skills + extra tools)."""
        return _skills_to_anthropic_tools(self.skills()) + self._extra_tools

    # ------------------------------------------------------------------
    # Extra tool wiring (e.g., route_to_agent for orchestrators)
    # ------------------------------------------------------------------
    def bind_extra_tool(self, schema: dict, handler: SkillCallHook) -> None:
        """Register an out-of-catalog tool and its handler on this agent."""
        self._extra_tools.append(schema)
        self._extra_tool_handlers[schema["name"]] = handler

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------
    def run(self, message: str) -> str:
        messages: list[dict] = [{"role": "user", "content": message}]
        tools = self.tools()

        for _ in range(self.max_steps):
            kwargs: dict[str, Any] = {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "system": self.system_prompt,
                "messages": messages,
            }
            if tools:
                kwargs["tools"] = tools
            response = self.client.messages.create(**kwargs)

            if response.stop_reason == "end_turn":
                for block in response.content:
                    if hasattr(block, "text"):
                        return block.text
                return ""

            if response.stop_reason != "tool_use":
                return ""

            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = self._dispatch(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result),
                    })
            messages.append({"role": "user", "content": tool_results})

        return "(step limit exceeded)"

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _dispatch(self, name: str, args: dict) -> Any:
        if name in self._extra_tool_handlers:
            return self._extra_tool_handlers[name](name, args)
        return self._on_skill_call(name, args)
