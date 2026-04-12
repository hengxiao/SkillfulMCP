"""
MCP catalog bootstrap + token utilities shared across framework runners.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import yaml


DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "network.yaml"


def admin_headers(admin_key: str) -> dict[str, str]:
    return {"X-Admin-Key": admin_key}


def load_network_config(path: Path | str | None = None) -> dict[str, Any]:
    """Load network.yaml (defaults to the one next to example/)."""
    cfg_path = Path(path) if path else DEFAULT_CONFIG
    return yaml.safe_load(cfg_path.read_text(encoding="utf-8"))


def bootstrap_mcp(config: dict, server_url: str, admin_key: str) -> None:
    """Idempotently provision skillsets, skills, and agents into the MCP server."""
    headers = admin_headers(admin_key)
    with httpx.Client(base_url=server_url, timeout=10) as http:
        # Skillsets + their skills.
        for ss_def in config.get("skillsets", []):
            http.put(
                f"/skillsets/{ss_def['id']}",
                json={
                    "id": ss_def["id"],
                    "name": ss_def["name"],
                    "description": ss_def.get("description", ""),
                },
                headers=headers,
            ).raise_for_status()

            for skill_def in ss_def.get("skills", []):
                resp = http.post(
                    "/skills",
                    json={
                        "id": skill_def["id"],
                        "name": skill_def["name"],
                        "description": skill_def.get("description", ""),
                        "version": skill_def["version"],
                        "metadata": skill_def.get("metadata", {}),
                        "skillset_ids": [],
                    },
                    headers=headers,
                )
                if resp.status_code not in (200, 201, 409):
                    resp.raise_for_status()
                http.put(
                    f"/skillsets/{ss_def['id']}/skills/{skill_def['id']}",
                    headers=headers,
                ).raise_for_status()

        # Agents.
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


def get_agent_token(server_url: str, agent_id: str, admin_key: str) -> str:
    with httpx.Client(base_url=server_url, timeout=10) as http:
        resp = http.post(
            "/token",
            json={"agent_id": agent_id, "expires_in": 3600},
            headers=admin_headers(admin_key),
        )
        resp.raise_for_status()
    return resp.json()["access_token"]


def load_agent_skills(server_url: str, token: str) -> list[dict]:
    """Return the skills the agent's JWT authorizes (list of SkillResponse)."""
    with httpx.Client(base_url=server_url, timeout=10) as http:
        resp = http.get("/skills", headers={"Authorization": f"Bearer {token}"})
        resp.raise_for_status()
    return resp.json()


def orchestrator_routing_tool_schema(worker_ids: list[str]) -> dict:
    """
    JSON-Schema-style definition for the `route_to_agent` meta-tool.

    Each framework adapts this into its native tool format, but the input
    schema is identical everywhere so the orchestrator system prompt can stay
    framework-agnostic.
    """
    return {
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
                    "enum": worker_ids,
                },
                "request_summary": {
                    "type": "string",
                    "description": "Concise summary of what the customer needs",
                },
            },
            "required": ["agent_id", "request_summary"],
        },
    }
