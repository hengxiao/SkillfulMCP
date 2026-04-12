"""
run_network.py — SkillfulMCP Agentic Network Runner
====================================================

Reads network.yaml to bootstrap the MCP catalog and instantiate a
multi-agent network powered by the Claude API.

Each agent in the network:
  - Holds a JWT token scoped to its assigned skillsets
  - Discovers its tools by calling GET /skills with that token
  - Builds Anthropic tool definitions from the skill metadata
  - Uses Claude (via the Anthropic SDK) to reason and call tools

The orchestrator agent receives the incoming message, calls the
classify_intent skill, then delegates to a specialist worker via an
internal `route_to_agent` meta-tool.

Prerequisites
-------------
  1. Install dependencies:
       pip install -e ".[dev]"

  2. Start the MCP server in another terminal:
       MCP_JWT_SECRET=example-secret MCP_ADMIN_KEY=admin-key mcp-server

  3. Set your Anthropic API key:
       export ANTHROPIC_API_KEY=sk-ant-...

  4. Run the example:
       MCP_ADMIN_KEY=admin-key python example/run_network.py \\
           --message "I have a billing question about invoice #1234"

Environment variables
---------------------
  ANTHROPIC_API_KEY   Required. Your Anthropic API key.
  MCP_SERVER_URL      MCP server base URL (default: http://localhost:8000).
  MCP_ADMIN_KEY       Admin key for the MCP server.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import anthropic
import httpx
import yaml
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# MCP server helpers
# ---------------------------------------------------------------------------

def _admin_headers(admin_key: str) -> dict:
    return {"X-Admin-Key": admin_key}


def bootstrap_mcp(config: dict, server_url: str, admin_key: str) -> None:
    """
    Idempotently provision skillsets, skills, and agents into the MCP server
    from the network.yaml definition.
    """
    headers = _admin_headers(admin_key)

    with httpx.Client(base_url=server_url, timeout=10) as http:
        for ss_def in config.get("skillsets", []):
            # Upsert skillset
            http.put(
                f"/skillsets/{ss_def['id']}",
                json={"id": ss_def["id"], "name": ss_def["name"],
                      "description": ss_def.get("description", "")},
                headers=headers,
            ).raise_for_status()

            # Upsert each skill in the skillset
            for skill_def in ss_def.get("skills", []):
                resp = http.post(
                    "/skills",
                    json={
                        "id": skill_def["id"],
                        "name": skill_def["name"],
                        "description": skill_def.get("description", ""),
                        "version": skill_def["version"],
                        "metadata": skill_def.get("metadata", {}),
                        "skillset_ids": [],       # associate separately below
                    },
                    headers=headers,
                )
                if resp.status_code not in (200, 201, 409):
                    resp.raise_for_status()

                # Associate skill → skillset (idempotent)
                http.put(
                    f"/skillsets/{ss_def['id']}/skills/{skill_def['id']}",
                    headers=headers,
                ).raise_for_status()

        # Register agents
        for agent_def in config.get("agents", []):
            resp = http.post(
                "/agents",
                json={
                    "id": agent_def["id"],
                    "name": agent_def["name"],
                    "skillsets": agent_def.get("skillsets", []),
                    "skills": agent_def.get("skills", []),
                    "scope": agent_def.get("scope", ["read"]),
                },
                headers=headers,
            )
            if resp.status_code not in (200, 201, 409):
                resp.raise_for_status()

    print(f"  Catalog provisioned from network.yaml.")


def get_agent_token(server_url: str, agent_id: str, admin_key: str) -> str:
    with httpx.Client(base_url=server_url, timeout=10) as http:
        resp = http.post(
            "/token",
            json={"agent_id": agent_id, "expires_in": 3600},
            headers=_admin_headers(admin_key),
        )
        resp.raise_for_status()
    return resp.json()["access_token"]


def load_agent_skills(server_url: str, token: str) -> list[dict]:
    """Fetch the skills the agent's JWT authorizes."""
    with httpx.Client(base_url=server_url, timeout=10) as http:
        resp = http.get("/skills", headers={"Authorization": f"Bearer {token}"})
        resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Tool building
# ---------------------------------------------------------------------------

def skills_to_tools(skills: list[dict]) -> list[dict]:
    """
    Convert MCP skill metadata into Anthropic tool definitions.

    Skill ids may contain hyphens; Anthropic tool names must match [a-zA-Z0-9_].
    """
    tools = []
    for skill in skills:
        tool_name = skill["id"].replace("-", "_")
        meta = skill.get("metadata") or {}
        input_schema = meta.get("input_schema") or {
            "type": "object",
            "properties": {},
        }
        tools.append({
            "name": tool_name,
            "description": skill.get("description") or skill["name"],
            "input_schema": input_schema,
        })
    return tools


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class MCPAgent:
    """
    A single agent backed by Claude and a JWT-scoped set of MCP skills.

    The agent runs a standard tool-use loop: send messages, handle
    tool_use blocks by calling _execute_tool, continue until end_turn.
    """

    def __init__(
        self,
        agent_def: dict,
        token: str,
        server_url: str,
        anthropic_client: anthropic.Anthropic,
    ) -> None:
        self.id: str = agent_def["id"]
        self.name: str = agent_def["name"]
        self.role: str = agent_def.get("role", "worker")
        self.model: str = agent_def.get("model", "claude-sonnet-4-6")
        self.system_prompt: str = agent_def.get("system_prompt", "")
        self.token = token
        self.server_url = server_url
        self.client = anthropic_client

        self.skills = load_agent_skills(server_url, token)
        self.tools = skills_to_tools(self.skills)
        print(f"  [{self.name}] loaded {len(self.skills)} skill(s): "
              f"{[s['id'] for s in self.skills]}")

    def run(self, message: str, extra_tools: list[dict] | None = None) -> str:
        """
        Run a single-turn conversation for this agent.

        extra_tools: additional meta-tools injected by the network
                     (e.g., route_to_agent for the orchestrator).
        """
        all_tools = self.tools + (extra_tools or [])
        messages: list[dict] = [{"role": "user", "content": message}]

        while True:
            kwargs: dict[str, Any] = {
                "model": self.model,
                "max_tokens": 1024,
                "system": self.system_prompt,
                "messages": messages,
            }
            if all_tools:
                kwargs["tools"] = all_tools

            response = self.client.messages.create(**kwargs)

            if response.stop_reason == "end_turn":
                for block in response.content:
                    if hasattr(block, "text"):
                        return block.text
                return ""

            if response.stop_reason == "tool_use":
                messages.append({"role": "assistant", "content": response.content})
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        result = self._execute_tool(block.name, block.input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result),
                        })
                messages.append({"role": "user", "content": tool_results})
            else:
                # Unexpected stop reason
                break

        return ""

    def _execute_tool(self, tool_name: str, tool_input: dict) -> dict:
        """
        Execute a skill tool call.

        In this demo the responses are simulated.  In a real system, each
        skill's metadata would include an endpoint or handler to invoke.
        """
        print(f"    [{self.name}] → {tool_name}({json.dumps(tool_input, ensure_ascii=False)})")

        simulated_responses: dict[str, dict] = {
            "classify_intent": {
                "intent": _infer_intent(tool_input.get("message", "")),
                "confidence": 0.91,
            },
            "lookup_invoice": {
                "invoices": [
                    {"invoice_id": "INV-1234", "amount": 49.99,
                     "status": "paid", "date": "2026-03-15"},
                    {"invoice_id": "INV-1198", "amount": 12.00,
                     "status": "pending", "date": "2026-04-01"},
                ]
            },
            "apply_credit": {
                "success": True,
                "credit_applied": tool_input.get("amount", 0),
                "new_balance": 0.00,
            },
            "run_diagnostic": {
                "status": "degraded",
                "issues": ["packet-loss-detected", "signal-below-threshold"],
                "recommendation": "Reboot the modem; if issue persists, schedule a technician.",
            },
            "schedule_technician": {
                "confirmation_id": "TECH-20260415-001",
                "scheduled_date": tool_input.get("preferred_date", "TBD"),
                "technician": "Field Team B",
            },
        }
        return simulated_responses.get(tool_name, {"result": "ok", "tool": tool_name})


def _infer_intent(message: str) -> str:
    """Simple keyword-based intent classifier for demo purposes."""
    msg = message.lower()
    if any(w in msg for w in ("bill", "invoice", "charge", "payment", "credit")):
        return "billing"
    if any(w in msg for w in ("internet", "wifi", "connection", "slow", "outage",
                               "signal", "technical", "not working")):
        return "technical-support"
    if any(w in msg for w in ("account", "password", "login", "profile")):
        return "account"
    return "general"


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

class AgentNetwork:
    """
    Manages a collection of MCPAgent instances and routes messages.

    Topology: one orchestrator + N workers.

    The orchestrator receives the user message.  When it calls the
    route_to_agent meta-tool, the network hands the request to the
    designated worker and returns that worker's response.
    """

    ROUTING_TOOL: dict = {
        "name": "route_to_agent",
        "description": (
            "Delegate the customer request to a specialist agent. "
            "Call this after classifying the intent."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "ID of the worker agent to route to",
                    "enum": [],   # populated at runtime
                },
                "request_summary": {
                    "type": "string",
                    "description": "Concise summary of what the customer needs",
                },
            },
            "required": ["agent_id", "request_summary"],
        },
    }

    def __init__(
        self,
        config: dict,
        server_url: str,
        admin_key: str,
        anthropic_client: anthropic.Anthropic,
    ) -> None:
        self.agents: dict[str, MCPAgent] = {}
        self._routing_log: list[dict] = []

        for agent_def in config.get("agents", []):
            token = get_agent_token(server_url, agent_def["id"], admin_key)
            agent = MCPAgent(agent_def, token, server_url, anthropic_client)
            self.agents[agent.id] = agent

        # Patch the routing tool with the real worker ids
        worker_ids = [
            a.id for a in self.agents.values() if a.role != "orchestrator"
        ]
        self.ROUTING_TOOL["input_schema"]["properties"]["agent_id"]["enum"] = worker_ids

    def _get_orchestrator(self) -> MCPAgent | None:
        for agent in self.agents.values():
            if agent.role == "orchestrator":
                return agent
        return None

    def _handle_routing(self, agent_id: str, request_summary: str) -> str:
        worker = self.agents.get(agent_id)
        if not worker:
            return f"Error: unknown agent '{agent_id}'"
        print(f"\n  [Network] routing → {worker.name}")
        self._routing_log.append({"to": agent_id, "summary": request_summary})
        return worker.run(request_summary)

    def run(self, user_message: str) -> str:
        orchestrator = self._get_orchestrator()
        if not orchestrator:
            # No orchestrator — send directly to first agent
            first = next(iter(self.agents.values()), None)
            return first.run(user_message) if first else "No agents configured."

        print(f"\n  [Network] → orchestrator: {orchestrator.name}")

        # Give the orchestrator a routing meta-tool that actually delegates
        # to the right worker inside this Python process.
        original_execute = orchestrator._execute_tool

        def patched_execute(tool_name: str, tool_input: dict) -> dict:
            if tool_name == "route_to_agent":
                worker_reply = self._handle_routing(
                    tool_input["agent_id"],
                    tool_input["request_summary"],
                )
                return {"worker_response": worker_reply}
            return original_execute(tool_name, tool_input)

        orchestrator._execute_tool = patched_execute  # type: ignore[method-assign]
        try:
            result = orchestrator.run(user_message, extra_tools=[self.ROUTING_TOOL])
        finally:
            orchestrator._execute_tool = original_execute  # type: ignore[method-assign]

        return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a SkillfulMCP agentic network against the Claude API.",
    )
    parser.add_argument(
        "--config",
        default=Path(__file__).parent / "network.yaml",
        type=Path,
        help="Path to network.yaml (default: ./network.yaml)",
    )
    parser.add_argument(
        "--message",
        required=True,
        help="Customer message to process through the network",
    )
    parser.add_argument(
        "--server-url",
        default=os.getenv("MCP_SERVER_URL", "http://localhost:8000"),
        help="MCP server base URL",
    )
    parser.add_argument(
        "--admin-key",
        default=os.getenv("MCP_ADMIN_KEY", ""),
        help="MCP admin key (X-Admin-Key)",
    )
    args = parser.parse_args()

    config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    server_url: str = (
        config.get("network", {}).get("mcp_server_url") or args.server_url
    )

    print(f"Bootstrapping MCP catalog at {server_url} …")
    bootstrap_mcp(config, server_url, args.admin_key)

    print("\nInitializing agent network …")
    anthropic_client = anthropic.Anthropic()
    network = AgentNetwork(config, server_url, args.admin_key, anthropic_client)

    print(f"\nUser message: {args.message!r}")
    print("-" * 60)
    result = network.run(args.message)
    print("-" * 60)
    print(f"\nFinal response:\n{result}")


if __name__ == "__main__":
    main()
