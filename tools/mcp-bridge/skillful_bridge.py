"""SkillfulMCP ↔ Claude Code bridge.

Speaks the Model Context Protocol over stdio so Claude Code can talk
to a deployed SkillfulMCP catalog as a first-class tool surface.
The model decides when to fetch a skill; the bridge hands back
metadata, contents, or installs the bundle locally on demand.

Tools exposed:
- list_skills(query?)         search the catalog by id/name/description
- get_skill(skill_id, ver?)   return SKILL.md inline
- download_skill(skill_id,
                 ver?,
                 require_signature?)
                              mirror the bundle into ~/.claude/skills/<id>/

Auth:
- MCP_CATALOG_URL    e.g. https://catalog.skillful-mcp.example.com
- MCP_CATALOG_TOKEN  agent JWT minted via /token (recommended) OR an
                     operator JWT from /admin/users/authenticate
- MCP_CATALOG_ADMIN_KEY  optional; falls back to admin-key auth when
                         the JWT isn't set. Convenient for dev only.

Signature verification (optional):
- If `require_signature=True` is passed to `download_skill`, the
  bridge fetches the catalog's verified flag (admin-key path
  required) before writing the bundle. Refuses to install on
  unverified rows.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import httpx

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool
except ImportError as exc:  # pragma: no cover — runtime install hint
    raise SystemExit(
        "the mcp Python package is required: pip install mcp\n"
        f"(import failed: {exc})"
    )


# ---------------------------------------------------------------------------
# Config (read from env at import time so misconfig surfaces immediately)
# ---------------------------------------------------------------------------

CATALOG = os.environ.get("MCP_CATALOG_URL", "").rstrip("/")
TOKEN = os.environ.get("MCP_CATALOG_TOKEN", "").strip()
ADMIN_KEY = os.environ.get("MCP_CATALOG_ADMIN_KEY", "").strip()
LOCAL_SKILLS = Path(
    os.environ.get("MCP_CATALOG_LOCAL_SKILLS", str(Path.home() / ".claude" / "skills"))
)


def _auth_headers() -> dict[str, str]:
    """JWT wins; admin key only if no JWT (dev-only path)."""
    if TOKEN:
        return {"Authorization": f"Bearer {TOKEN}"}
    if ADMIN_KEY:
        return {"X-Admin-Key": ADMIN_KEY}
    return {}


def _validate_config() -> None:
    if not CATALOG:
        raise SystemExit("MCP_CATALOG_URL is required")
    if not TOKEN and not ADMIN_KEY:
        raise SystemExit(
            "either MCP_CATALOG_TOKEN (preferred) or MCP_CATALOG_ADMIN_KEY "
            "must be set"
        )


# ---------------------------------------------------------------------------
# Tool handlers — pure functions over an httpx.AsyncClient so they can be
# unit-tested with httpx.MockTransport without spinning up the MCP server.
# ---------------------------------------------------------------------------

def _is_admin_key_path() -> bool:
    """When the bridge is admin-key-authenticated, list / get use the
    admin endpoints which return more fields (e.g. `verified`)."""
    return not TOKEN and bool(ADMIN_KEY)


def _list_path() -> str:
    return "/admin/skills" if _is_admin_key_path() else "/skills"


def _detail_path(skill_id: str) -> str:
    return f"/admin/skills/{skill_id}" if _is_admin_key_path() else f"/skills/{skill_id}"


def _files_path(skill_id: str, version: str) -> str:
    # Same path on both surfaces.
    base = "/admin/skills" if _is_admin_key_path() else "/skills"
    return f"{base}/{skill_id}/versions/{version}/files"


async def _do_list_skills(client: httpx.AsyncClient, query: str | None) -> str:
    r = await client.get(CATALOG + _list_path())
    r.raise_for_status()
    rows = r.json()
    q = (query or "").lower().strip()
    if q:
        rows = [
            s for s in rows
            if q in s.get("id", "").lower()
            or q in s.get("name", "").lower()
            or q in (s.get("description") or "").lower()
        ]
    if not rows:
        return "No skills matched."
    lines = []
    for s in rows[:50]:
        verified = s.get("verified")
        marker = " ✓" if verified is True else (" ⚠" if verified is False else "")
        lines.append(
            f"- {s['id']} ({s['version']}){marker} — {s.get('name', s['id'])}"
            + (f"\n    {s['description']}" if s.get("description") else "")
        )
    return "\n".join(lines)


async def _do_get_skill(
    client: httpx.AsyncClient, skill_id: str, version: str | None
) -> str:
    """Return the SKILL.md body. Resolves latest version when omitted."""
    detail = (await client.get(
        CATALOG + _detail_path(skill_id),
        params={"version": version} if version else None,
    )).json()
    ver = detail.get("version")
    if not ver:
        raise ValueError(f"skill {skill_id!r} not found")
    files_url = CATALOG + _files_path(skill_id, ver) + "/SKILL.md"
    r = await client.get(files_url)
    r.raise_for_status()
    body = r.text
    header = (
        f"# {detail.get('name', skill_id)} (v{ver})\n"
        f"id: `{skill_id}`\n"
        f"visibility: {detail.get('visibility', 'unknown')}\n"
    )
    return header + "\n---\n" + body


async def _do_download_skill(
    client: httpx.AsyncClient,
    skill_id: str,
    version: str | None,
    require_signature: bool,
) -> str:
    detail = (await client.get(
        CATALOG + _detail_path(skill_id),
        params={"version": version} if version else None,
    )).json()
    ver = detail.get("version")
    if not ver:
        raise ValueError(f"skill {skill_id!r} not found")

    if require_signature:
        # `verified` is only computed on the admin-key /admin/skills/{id}
        # path. With operator-JWT auth, switch the bridge to admin-key
        # mode (MCP_CATALOG_ADMIN_KEY) for this guarantee.
        verified = detail.get("verified")
        if verified is None:
            raise ValueError(
                "require_signature=True needs MCP_CATALOG_ADMIN_KEY auth "
                "(JWT response doesn't carry the `verified` flag)"
            )
        if verified is not True:
            raise ValueError(
                f"refusing to install {skill_id!r} {ver} — bundle "
                "signature did not verify"
            )

    files = (await client.get(CATALOG + _files_path(skill_id, ver))).json()
    dest = LOCAL_SKILLS / skill_id
    dest.mkdir(parents=True, exist_ok=True)

    # Wipe any previous version so stale files don't linger.
    for existing in dest.rglob("*"):
        if existing.is_file():
            existing.unlink()

    for f in files:
        path = dest / f["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        r = await client.get(
            CATALOG + _files_path(skill_id, ver) + "/" + f["path"]
        )
        r.raise_for_status()
        path.write_bytes(r.content)
    return (
        f"installed {skill_id} v{ver} → {dest} "
        f"({len(files)} file{'s' if len(files) != 1 else ''})"
    )


# ---------------------------------------------------------------------------
# MCP server wiring
# ---------------------------------------------------------------------------

def make_server() -> Server:
    server = Server("skillful-mcp-bridge")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="list_skills",
                description=(
                    "Search the SkillfulMCP catalog. Returns one line per "
                    "skill (id, version, name, optional description). When "
                    "the bridge is admin-key authenticated, each row also "
                    "shows ✓ (signature verified) or ⚠ (unverified)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "Optional substring; matched case-insensitively "
                                "against skill id, name, and description."
                            ),
                        },
                    },
                },
            ),
            Tool(
                name="get_skill",
                description=(
                    "Return the SKILL.md instructions for a skill without "
                    "writing it to disk. Use this to peek before deciding "
                    "whether to download_skill."
                ),
                inputSchema={
                    "type": "object",
                    "required": ["skill_id"],
                    "properties": {
                        "skill_id": {"type": "string"},
                        "version": {
                            "type": "string",
                            "description": (
                                "Specific version (semver). Omit for latest."
                            ),
                        },
                    },
                },
            ),
            Tool(
                name="download_skill",
                description=(
                    "Mirror the full bundle into ~/.claude/skills/<skill_id>/ "
                    "so Claude Code's native skill discovery picks it up. "
                    "Returns a one-line install summary."
                ),
                inputSchema={
                    "type": "object",
                    "required": ["skill_id"],
                    "properties": {
                        "skill_id": {"type": "string"},
                        "version": {"type": "string"},
                        "require_signature": {
                            "type": "boolean",
                            "description": (
                                "When true, refuse to install bundles whose "
                                "Ed25519 signature didn't verify against the "
                                "catalog's trust store. Requires admin-key "
                                "auth (verified flag isn't on the JWT path)."
                            ),
                            "default": False,
                        },
                    },
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]):
        async with httpx.AsyncClient(headers=_auth_headers(), timeout=15) as c:
            try:
                if name == "list_skills":
                    text = await _do_list_skills(c, arguments.get("query"))
                elif name == "get_skill":
                    text = await _do_get_skill(
                        c, arguments["skill_id"], arguments.get("version")
                    )
                elif name == "download_skill":
                    text = await _do_download_skill(
                        c,
                        arguments["skill_id"],
                        arguments.get("version"),
                        bool(arguments.get("require_signature", False)),
                    )
                else:
                    text = f"unknown tool: {name}"
            except httpx.HTTPStatusError as exc:
                text = (
                    f"catalog returned {exc.response.status_code}: "
                    f"{exc.response.text[:200]}"
                )
            except Exception as exc:
                text = f"{type(exc).__name__}: {exc}"
            return [TextContent(type="text", text=text)]

    return server


async def _async_main() -> None:
    _validate_config()
    server = make_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


def main() -> None:  # pragma: no cover — entry point only
    asyncio.run(_async_main())


if __name__ == "__main__":  # pragma: no cover
    main()
