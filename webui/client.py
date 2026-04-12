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

    async def get_skill(self, skill_id: str) -> dict:
        return await self._request("GET", f"/admin/skills/{skill_id}")

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

    # ------------------------------------------------------------------
    # Agents (counts only — for dashboard)
    # ------------------------------------------------------------------

    async def list_agents(self) -> list[dict]:
        return await self._request("GET", "/agents")
