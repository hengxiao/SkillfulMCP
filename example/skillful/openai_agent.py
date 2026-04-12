"""
SkillfulOpenAIAgent — a reusable OpenAI-backed agent that sources its tools
from SkillfulMCP.

Example::

    agent = SkillfulOpenAIAgent(
        agent_id="billing-agent",
        server_url="http://localhost:8000",
        admin_key=os.environ["MCP_ADMIN_KEY"],
        client=OpenAI(),
        model="gpt-4o-mini",
        system_prompt="You are a billing specialist.",
    )
    reply = agent.run("Look up invoice INV-1234")
"""

from __future__ import annotations

import json
from typing import Any

from openai import OpenAI

from ._base import SkillCallHook, SkillFetcher, default_skill_call


def _skills_to_openai_tools(skills: list[dict]) -> list[dict]:
    tools = []
    for skill in skills:
        meta = skill.get("metadata") or {}
        tools.append({
            "type": "function",
            "function": {
                "name": skill["id"].replace("-", "_"),
                "description": skill.get("description") or skill["name"],
                "parameters": meta.get("input_schema") or {
                    "type": "object",
                    "properties": {},
                },
            },
        })
    return tools


class SkillfulOpenAIAgent:
    """OpenAI-backed agent with tools sourced from SkillfulMCP."""

    def __init__(
        self,
        *,
        agent_id: str,
        server_url: str,
        admin_key: str,
        client: OpenAI | None = None,
        model: str = "gpt-4o-mini",
        system_prompt: str = "",
        max_steps: int = 10,
        on_skill_call: SkillCallHook | None = None,
    ) -> None:
        self._fetcher = SkillFetcher(agent_id, server_url, admin_key)
        self.client = client or OpenAI()
        self.model = model
        self.system_prompt = system_prompt
        self.max_steps = max_steps
        self._on_skill_call = on_skill_call or default_skill_call
        self._extra_tools: list[dict] = []
        self._extra_tool_handlers: dict[str, SkillCallHook] = {}

    @property
    def agent_id(self) -> str:
        return self._fetcher.agent_id

    def skills(self) -> list[dict]:
        return self._fetcher.skills()

    def tools(self) -> list[dict]:
        return _skills_to_openai_tools(self.skills()) + self._extra_tools

    def bind_extra_tool(self, schema: dict, handler: SkillCallHook) -> None:
        """
        Register an out-of-catalog tool. `schema` uses the framework-agnostic
        shape (same as `orchestrator_routing_tool_schema`); it's converted to
        the OpenAI function format here.
        """
        openai_schema = {
            "type": "function",
            "function": {
                "name": schema["name"],
                "description": schema["description"],
                "parameters": schema["input_schema"],
            },
        }
        self._extra_tools.append(openai_schema)
        self._extra_tool_handlers[schema["name"]] = handler

    def run(self, message: str) -> str:
        tools = self.tools()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": message},
        ]

        for _ in range(self.max_steps):
            kwargs: dict[str, Any] = {"model": self.model, "messages": messages}
            if tools:
                kwargs["tools"] = tools
            resp = self.client.chat.completions.create(**kwargs)
            msg = resp.choices[0].message

            if not msg.tool_calls:
                return msg.content or ""

            # OpenAI requires the assistant message carrying tool_calls to
            # appear in history before the tool-result messages.
            messages.append({
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
            })
            for tc in msg.tool_calls:
                args = json.loads(tc.function.arguments or "{}")
                result = self._dispatch(tc.function.name, args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result),
                })

        return "(step limit exceeded)"

    def _dispatch(self, name: str, args: dict) -> Any:
        if name in self._extra_tool_handlers:
            return self._extra_tool_handlers[name](name, args)
        return self._on_skill_call(name, args)
