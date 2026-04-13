"""
Async HTTP client that wraps all calls to the MCP server API.
The web UI never touches the database directly — everything goes through here.
"""

from typing import Any

import httpx

from .config import get_settings


class MCPError(Exception):
    """Raised when the MCP server returns an error response."""

    def __init__(self, detail: str, status_code: int = 0) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def _extract_detail(exc: httpx.HTTPStatusError) -> str:
    try:
        return exc.response.json().get("detail", exc.response.text)
    except Exception:
        return exc.response.text or str(exc)


class MCPClient:
    def __init__(self) -> None:
        s = get_settings()
        self._base_url = s.mcp_server_url
        self._headers = {"X-Admin-Key": s.admin_key}

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        async with httpx.AsyncClient(base_url=self._base_url, timeout=10) as c:
            try:
                r = await c.request(method, path, headers=self._headers, **kwargs)
                r.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise MCPError(_extract_detail(exc), exc.response.status_code) from exc
            except httpx.RequestError as exc:
                raise MCPError(
                    f"Could not reach MCP server at {self._base_url}: {exc}"
                ) from exc
        if r.status_code == 204:
            return None
        return r.json()

    # ------------------------------------------------------------------
    # Skillsets
    # ------------------------------------------------------------------

    async def list_skillsets(self) -> list[dict]:
        return await self._request("GET", "/skillsets")

    async def get_skillset(self, skillset_id: str) -> dict:
        return await self._request("GET", f"/skillsets/{skillset_id}")

    async def create_skillset(self, data: dict) -> dict:
        return await self._request("POST", "/skillsets", json=data)

    async def update_skillset(self, skillset_id: str, data: dict) -> dict:
        return await self._request("PUT", f"/skillsets/{skillset_id}", json=data)

    async def delete_skillset(self, skillset_id: str) -> None:
        await self._request("DELETE", f"/skillsets/{skillset_id}")

    async def list_skillset_skills(self, skillset_id: str) -> list[dict]:
        """Admin endpoint — no JWT required."""
        return await self._request("GET", f"/admin/skillsets/{skillset_id}/skills")

    async def associate_skill(self, skillset_id: str, skill_id: str) -> None:
        await self._request("PUT", f"/skillsets/{skillset_id}/skills/{skill_id}")

    async def disassociate_skill(self, skillset_id: str, skill_id: str) -> None:
        await self._request("DELETE", f"/skillsets/{skillset_id}/skills/{skill_id}")

    # ------------------------------------------------------------------
    # Skills (admin endpoints — no JWT required)
    # ------------------------------------------------------------------

    async def list_skills(self) -> list[dict]:
        return await self._request("GET", "/admin/skills")

    async def get_skill(self, skill_id: str, version: str | None = None) -> dict:
        params = {"version": version} if version else {}
        return await self._request(
            "GET", f"/admin/skills/{skill_id}", params=params
        )

    async def list_skill_versions(self, skill_id: str) -> list[dict]:
        return await self._request("GET", f"/admin/skills/{skill_id}/versions")

    async def create_skill(self, data: dict) -> dict:
        return await self._request("POST", "/skills", json=data)

    async def update_skill(self, skill_id: str, data: dict) -> dict:
        return await self._request("PUT", f"/skills/{skill_id}", json=data)

    async def delete_skill(self, skill_id: str, version: str | None = None) -> None:
        params = {"version": version} if version else {}
        await self._request("DELETE", f"/skills/{skill_id}", params=params)

    # ------------------------------------------------------------------
    # Bundles (admin endpoints)
    # ------------------------------------------------------------------

    async def list_bundle_files(self, skill_id: str, version: str) -> list[dict]:
        return await self._request(
            "GET", f"/admin/skills/{skill_id}/versions/{version}/files"
        )

    async def get_bundle_file(self, skill_id: str, version: str, path: str) -> bytes:
        """Return raw bytes for a single file in a bundle."""
        async with httpx.AsyncClient(base_url=self._base_url, timeout=30) as c:
            r = await c.get(
                f"/admin/skills/{skill_id}/versions/{version}/files/{path}",
                headers=self._headers,
            )
            r.raise_for_status()
            return r.content

    async def upload_bundle(
        self, skill_id: str, version: str, filename: str, data: bytes
    ) -> dict:
        async with httpx.AsyncClient(base_url=self._base_url, timeout=60) as c:
            r = await c.post(
                f"/skills/{skill_id}/versions/{version}/bundle",
                headers=self._headers,
                files={"file": (filename, data, "application/octet-stream")},
            )
            try:
                r.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise MCPError(_extract_detail(exc), exc.response.status_code) from exc
            return r.json()

    async def delete_bundle(self, skill_id: str, version: str) -> None:
        await self._request(
            "DELETE", f"/skills/{skill_id}/versions/{version}/bundle"
        )

    async def copy_bundle(
        self,
        dst_skill_id: str,
        dst_version: str,
        src_skill_id: str,
        src_version: str,
    ) -> dict:
        return await self._request(
            "POST",
            f"/skills/{dst_skill_id}/versions/{dst_version}"
            f"/bundle/copy-from/{src_skill_id}/{src_version}",
        )

    # ------------------------------------------------------------------
    # Agents (counts only — for dashboard)
    # ------------------------------------------------------------------

    async def list_agents(self) -> list[dict]:
        return await self._request("GET", "/agents")

    async def get_agent(self, agent_id: str) -> dict:
        return await self._request("GET", f"/agents/{agent_id}")

    async def issue_token(self, data: dict) -> dict:
        """Wave 8c: `data` may carry optional `skills` / `skillsets` /
        `scope` narrowing lists."""
        return await self._request("POST", "/token", json=data)

    # ------------------------------------------------------------------
    # Users (admin endpoints — Wave 8b)
    # ------------------------------------------------------------------

    async def list_users(self) -> list[dict]:
        return await self._request("GET", "/admin/users")

    async def get_user(self, user_id: str) -> dict:
        return await self._request("GET", f"/admin/users/{user_id}")

    async def create_user(self, data: dict) -> dict:
        return await self._request("POST", "/admin/users", json=data)

    async def update_user(self, user_id: str, data: dict) -> dict:
        return await self._request("PUT", f"/admin/users/{user_id}", json=data)

    async def delete_user(self, user_id: str) -> None:
        await self._request("DELETE", f"/admin/users/{user_id}")

    # ------------------------------------------------------------------
    # Accounts + memberships (Wave 9.1)
    # ------------------------------------------------------------------

    async def list_accounts(self) -> list[dict]:
        return await self._request("GET", "/admin/accounts")

    async def get_account(self, account_id: str) -> dict:
        return await self._request("GET", f"/admin/accounts/{account_id}")

    async def create_account(self, name: str, initial_admin_user_id: str) -> dict:
        return await self._request(
            "POST",
            "/admin/accounts",
            json={
                "name": name,
                "initial_admin_user_id": initial_admin_user_id,
            },
        )

    async def delete_account(
        self,
        account_id: str,
        *,
        confirm_user_count: int,
        confirm_skill_count: int = 0,
        confirm_skillset_count: int = 0,
        confirm_agent_count: int = 0,
        cascade_catalog: bool = False,
    ) -> None:
        await self._request(
            "DELETE",
            f"/admin/accounts/{account_id}",
            params={
                "confirm_user_count": confirm_user_count,
                "confirm_skill_count": confirm_skill_count,
                "confirm_skillset_count": confirm_skillset_count,
                "confirm_agent_count": confirm_agent_count,
                "cascade_catalog": 1 if cascade_catalog else 0,
            },
        )

    async def list_members(self, account_id: str) -> list[dict]:
        """Combined active + pending list. Rows are tagged by a
        `pending: bool` field."""
        return await self._request(
            "GET", f"/admin/accounts/{account_id}/members"
        )

    async def invite_member(
        self, account_id: str, email: str, role: str
    ) -> dict:
        return await self._request(
            "POST",
            f"/admin/accounts/{account_id}/members",
            json={"email": email, "role": role},
        )

    async def update_member_role(
        self, account_id: str, user_id: str, role: str
    ) -> dict:
        return await self._request(
            "PUT",
            f"/admin/accounts/{account_id}/members/{user_id}",
            json={"role": role},
        )

    async def remove_member(
        self,
        account_id: str,
        user_id: str,
        *,
        new_owner_id: str | None = None,
    ) -> None:
        params: dict = {}
        if new_owner_id:
            params["new_owner_id"] = new_owner_id
        await self._request(
            "DELETE",
            f"/admin/accounts/{account_id}/members/{user_id}",
            params=params,
        )

    async def delete_pending_invite(
        self, account_id: str, pending_id: int
    ) -> None:
        await self._request(
            "DELETE",
            f"/admin/accounts/{account_id}/pending/{pending_id}",
        )

    # ------------------------------------------------------------------
    # Signup + disable (Wave 9.1)
    # ------------------------------------------------------------------

    async def signup(
        self, email: str, password: str, display_name: str | None = None
    ) -> dict:
        return await self._request(
            "POST",
            "/admin/signup",
            json={
                "email": email,
                "password": password,
                "display_name": display_name,
            },
        )

    async def set_user_disabled(self, user_id: str, disabled: bool) -> dict:
        return await self._request(
            "PUT",
            f"/admin/users/{user_id}/disable",
            json={"disabled": disabled},
        )

    async def authenticate_user(self, email: str, password: str) -> dict | None:
        """Returns the user dict on success, None on 401.

        Anything other than 200/401 (transport errors, 500s) bubbles up
        as MCPError so the caller can decide whether to fall back to env
        operators.
        """
        try:
            return await self._request(
                "POST",
                "/admin/users/authenticate",
                json={"email": email, "password": password},
            )
        except MCPError as exc:
            if exc.status_code == 401:
                return None
            raise
