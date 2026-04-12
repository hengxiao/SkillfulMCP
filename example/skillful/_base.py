"""
Shared skill-fetching scaffolding for the per-framework agent classes.

A `SkillFetcher` holds the catalog URL + admin key + agent id, and exposes
`skills()` which mints a JWT (once) and fetches the authorized skill list
(once). All four Skillful* agent classes delegate to one of these.
"""

from __future__ import annotations

from typing import Callable

from example.common import (
    dispatch_skill,
    get_agent_token,
    load_agent_skills,
)


class SkillFetcher:
    """
    Lazily authenticates as `agent_id` against the catalog and caches the
    resulting JWT + skill list.

    Parameters
    ----------
    agent_id:
        Catalog id of the agent to impersonate.
    server_url:
        Base URL of the SkillfulMCP server (e.g. http://localhost:8000).
    admin_key:
        Admin key used to mint a JWT on behalf of `agent_id`.
    """

    def __init__(self, agent_id: str, server_url: str, admin_key: str) -> None:
        self.agent_id = agent_id
        self.server_url = server_url
        self.admin_key = admin_key
        self._token: str | None = None
        self._skills: list[dict] | None = None

    @property
    def token(self) -> str:
        if self._token is None:
            self._token = get_agent_token(self.server_url, self.agent_id, self.admin_key)
        return self._token

    def skills(self) -> list[dict]:
        """Return the skills the agent's JWT authorizes. Cached after first call."""
        if self._skills is None:
            self._skills = load_agent_skills(self.server_url, self.token)
        return self._skills


# Type alias used by all Skillful* agents.
#   (tool_name, tool_input) -> tool result dict
SkillCallHook = Callable[[str, dict], dict]


def default_skill_call(name: str, args: dict) -> dict:
    """
    Default tool-call handler. Delegates to the simulated dispatcher used by
    the demo runners. Production deployments should pass their own
    `on_skill_call` to the Skillful* agent constructor.
    """
    return dispatch_skill(name, args)
