"""
LangGraph runner — uses `SkillfulLangGraphAgent`.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic

from example.common import bootstrap_mcp, load_network_config
from example.skillful import SkillfulLangGraphAgent
from example.skillful._network import wire_orchestrator

load_dotenv()

DEFAULT_MODEL = "claude-sonnet-4-6"


def build_agents(config, server_url, admin_key, llm):
    agents: dict[str, SkillfulLangGraphAgent] = {}
    roles: dict[str, str] = {}
    for a in config.get("agents", []):
        agent = SkillfulLangGraphAgent(
            agent_id=a["id"],
            server_url=server_url,
            admin_key=admin_key,
            llm=llm,
            system_prompt=a.get("system_prompt", ""),
        )
        agents[a["id"]] = agent
        roles[a["id"]] = a.get("role", "worker")
        print(f"  [{a['name']}] loaded {len(agent.skills())} skill(s): "
              f"{[s['id'] for s in agent.skills()]}")
    return agents, roles


def main() -> None:
    parser = argparse.ArgumentParser(description="Run via LangGraph.")
    parser.add_argument("--message", required=True)
    parser.add_argument("--server-url", default=os.getenv("MCP_SERVER_URL", "http://localhost:8000"))
    parser.add_argument("--admin-key", default=os.getenv("MCP_ADMIN_KEY", ""))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()

    config = load_network_config()
    server_url = config.get("network", {}).get("mcp_server_url") or args.server_url

    print(f"Bootstrapping MCP catalog at {server_url} …")
    bootstrap_mcp(config, server_url, args.admin_key)

    print("\nInitializing agents (LangGraph) …")
    llm = ChatAnthropic(model=args.model, max_tokens=1024)
    agents, roles = build_agents(config, server_url, args.admin_key, llm)

    orch_id = next(i for i, r in roles.items() if r == "orchestrator")
    orchestrator = agents[orch_id]
    workers = {i: a for i, a in agents.items() if roles[i] != "orchestrator"}
    wire_orchestrator(orchestrator, workers)

    print(f"\n  [Network] → orchestrator: {orch_id}")
    print(f"\nUser message: {args.message!r}")
    print("-" * 60)
    result = orchestrator.run(args.message)
    print("-" * 60)
    print(f"\nFinal response:\n{result}")


if __name__ == "__main__":
    main()
