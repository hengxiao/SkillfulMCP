"""
Shared helpers used by every framework-specific runner.

Each runner under `example/<framework>/run_network.py` uses these to:

- provision the MCP catalog (skills, skillsets, agents) from network.yaml,
- issue per-agent JWTs,
- fetch the skills each JWT authorizes,
- dispatch skill calls to simulated responses.

The framework-specific runner is then responsible only for translating skills
into whatever tool/function/agent abstraction the framework uses, and for
driving the orchestrator → worker flow.
"""

from .mcp_bootstrap import (
    admin_headers,
    bootstrap_mcp,
    get_agent_token,
    load_agent_skills,
    load_network_config,
    orchestrator_routing_tool_schema,
)
from .skill_dispatcher import dispatch_skill

__all__ = [
    "admin_headers",
    "bootstrap_mcp",
    "get_agent_token",
    "load_agent_skills",
    "load_network_config",
    "orchestrator_routing_tool_schema",
    "dispatch_skill",
]
