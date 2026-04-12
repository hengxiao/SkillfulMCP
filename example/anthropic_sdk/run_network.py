"""
Anthropic runner — uses `SkillfulAnthropicAgent` so each agent fetches its
skills from the catalog on first run.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Make `example.*` importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import anthropic
from dotenv import load_dotenv

from example.common import bootstrap_mcp, load_network_config
from example.skillful import SkillfulAnthropicAgent
from example.skillful._network import wire_orchestrator

load_dotenv()


def build_agents(config: dict, server_url: str, admin_key: str, client: anthropic.Anthropic):
    agents: dict[str, SkillfulAnthropicAgent] = {}
    roles: dict[str, str] = {}
    for a in config.get("agents", []):
        agent = SkillfulAnthropicAgent(
            agent_id=a["id"],
            server_url=server_url,
            admin_key=admin_key,
            client=client,
            model=a.get("model", "claude-sonnet-4-6"),
            system_prompt=a.get("system_prompt", ""),
        )
        agents[a["id"]] = agent
        roles[a["id"]] = a.get("role", "worker")
        # Force skill fetch so bootstrap issues surface at startup, not mid-run.
        print(f"  [{a['name']}] loaded {len(agent.skills())} skill(s): "
              f"{[s['id'] for s in agent.skills()]}")
    return agents, roles


def main() -> None:
    parser = argparse.ArgumentParser(description="Run via the Anthropic SDK.")
    parser.add_argument("--message", required=True)
    parser.add_argument("--server-url", default=os.getenv("MCP_SERVER_URL", "http://localhost:8000"))
    parser.add_argument("--admin-key", default=os.getenv("MCP_ADMIN_KEY", ""))
    args = parser.parse_args()

    config = load_network_config()
    server_url = config.get("network", {}).get("mcp_server_url") or args.server_url

    print(f"Bootstrapping MCP catalog at {server_url} …")
    bootstrap_mcp(config, server_url, args.admin_key)

    print("\nInitializing agents (Anthropic SDK) …")
    agents, roles = build_agents(config, server_url, args.admin_key, anthropic.Anthropic())

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
