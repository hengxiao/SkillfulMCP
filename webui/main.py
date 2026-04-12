"""
SkillfulMCP Web UI — FastAPI application.

All data flows through MCPClient, which proxies to the MCP server API.
Forms use standard POST + redirect (PRG) for create/update operations.
Deletes use HTMX so rows are removed inline without a full page reload.
Flash messages are passed as ?msg=...&msg_type=success|error query params.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

import uvicorn
from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .client import MCPClient, MCPError
from .config import get_settings

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_client: MCPClient | None = None


def get_client() -> MCPClient:
    global _client
    if _client is None:
        _client = MCPClient()
    return _client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _redirect(path: str, msg: str = "", msg_type: str = "success") -> RedirectResponse:
    url = f"{path}?msg={quote(msg)}&msg_type={msg_type}" if msg else path
    return RedirectResponse(url, status_code=303)


def _render(request: Request, template: str, ctx: dict) -> HTMLResponse:
    ctx["request"] = request
    return templates.TemplateResponse(template, ctx)


def _flash_ctx(msg: str, msg_type: str) -> dict:
    return {"msg": msg, "msg_type": msg_type}


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app() -> FastAPI:
    app = FastAPI(title="SkillfulMCP Web UI", docs_url=None, redoc_url=None)

    # ------------------------------------------------------------------ #
    # Dashboard                                                           #
    # ------------------------------------------------------------------ #

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request, msg: str = "", msg_type: str = "success"):
        client = get_client()
        try:
            skillsets = await client.list_skillsets()
            skills = await client.list_skills()
            agents = await client.list_agents()
            error = None
        except MCPError as exc:
            skillsets, skills, agents = [], [], []
            error = str(exc)
        return _render(request, "dashboard.html", {
            "active": "dashboard",
            "skillsets_count": len(skillsets),
            "skills_count": len(skills),
            "agents_count": len(agents),
            "error": error,
            **_flash_ctx(msg, msg_type),
        })

    # ------------------------------------------------------------------ #
    # Skillsets — list + create                                           #
    # ------------------------------------------------------------------ #

    @app.get("/skillsets", response_class=HTMLResponse)
    async def skillsets_page(request: Request, msg: str = "", msg_type: str = "success"):
        client = get_client()
        try:
            skillsets = await client.list_skillsets()
            error = None
        except MCPError as exc:
            skillsets, error = [], str(exc)
        return _render(request, "skillsets.html", {
            "active": "skillsets",
            "skillsets": skillsets,
            "error": error,
            **_flash_ctx(msg, msg_type),
        })

    @app.post("/skillsets")
    async def create_skillset(
        id: Annotated[str, Form()],
        name: Annotated[str, Form()],
        description: Annotated[str, Form()] = "",
    ):
        try:
            await get_client().create_skillset(
                {"id": id, "name": name, "description": description}
            )
            return _redirect("/skillsets", f"Skillset '{id}' created.")
        except MCPError as exc:
            return _redirect("/skillsets", str(exc), "error")

    # ------------------------------------------------------------------ #
    # Skillset detail — view + edit + skill associations                  #
    # ------------------------------------------------------------------ #

    @app.get("/skillsets/{skillset_id}", response_class=HTMLResponse)
    async def skillset_detail(
        request: Request,
        skillset_id: str,
        msg: str = "",
        msg_type: str = "success",
    ):
        client = get_client()
        try:
            skillset = await client.get_skillset(skillset_id)
            member_skills = await client.list_skillset_skills(skillset_id)
            all_skills = await client.list_skills()
            member_ids = {s["id"] for s in member_skills}
            available_skills = [s for s in all_skills if s["id"] not in member_ids]
            error = None
        except MCPError as exc:
            return _redirect("/skillsets", str(exc), "error")
        return _render(request, "skillset.html", {
            "active": "skillsets",
            "skillset": skillset,
            "member_skills": member_skills,
            "available_skills": available_skills,
            "error": error,
            **_flash_ctx(msg, msg_type),
        })

    @app.post("/skillsets/{skillset_id}/update")
    async def update_skillset(
        skillset_id: str,
        name: Annotated[str, Form()],
        description: Annotated[str, Form()] = "",
    ):
        try:
            await get_client().update_skillset(
                skillset_id, {"id": skillset_id, "name": name, "description": description}
            )
            return _redirect(f"/skillsets/{skillset_id}", "Skillset updated.")
        except MCPError as exc:
            return _redirect(f"/skillsets/{skillset_id}", str(exc), "error")

    @app.delete("/skillsets/{skillset_id}")
    async def delete_skillset(skillset_id: str):
        try:
            await get_client().delete_skillset(skillset_id)
            return Response(status_code=200)
        except MCPError:
            return Response(status_code=500)

    @app.post("/skillsets/{skillset_id}/skills")
    async def associate_skill(
        skillset_id: str,
        skill_id: Annotated[str, Form()],
    ):
        try:
            await get_client().associate_skill(skillset_id, skill_id)
            return _redirect(f"/skillsets/{skillset_id}", f"Skill '{skill_id}' added.")
        except MCPError as exc:
            return _redirect(f"/skillsets/{skillset_id}", str(exc), "error")

    @app.delete("/skillsets/{skillset_id}/skills/{skill_id}")
    async def disassociate_skill(skillset_id: str, skill_id: str):
        try:
            await get_client().disassociate_skill(skillset_id, skill_id)
            return Response(status_code=200)
        except MCPError:
            return Response(status_code=500)

    # ------------------------------------------------------------------ #
    # Skills — list + create                                              #
    # ------------------------------------------------------------------ #

    @app.get("/skills", response_class=HTMLResponse)
    async def skills_page(request: Request, msg: str = "", msg_type: str = "success"):
        client = get_client()
        try:
            skills = await client.list_skills()
            skillsets = await client.list_skillsets()
            error = None
        except MCPError as exc:
            skills, skillsets, error = [], [], str(exc)
        return _render(request, "skills.html", {
            "active": "skills",
            "skills": skills,
            "skillsets": skillsets,
            "error": error,
            **_flash_ctx(msg, msg_type),
        })

    @app.post("/skills")
    async def create_skill(
        id: Annotated[str, Form()],
        name: Annotated[str, Form()],
        version: Annotated[str, Form()],
        description: Annotated[str, Form()] = "",
        metadata: Annotated[str, Form()] = "{}",
        skillset_ids: Annotated[list[str], Form()] = [],
    ):
        try:
            meta = json.loads(metadata)
        except json.JSONDecodeError:
            return _redirect("/skills", "metadata must be valid JSON.", "error")
        try:
            await get_client().create_skill({
                "id": id, "name": name, "description": description,
                "version": version, "metadata": meta, "skillset_ids": skillset_ids,
            })
            return _redirect("/skills", f"Skill '{id}' v{version} created.")
        except MCPError as exc:
            return _redirect("/skills", str(exc), "error")

    # ------------------------------------------------------------------ #
    # Skill detail — view + edit latest + version management             #
    # ------------------------------------------------------------------ #

    @app.get("/skills/{skill_id}", response_class=HTMLResponse)
    async def skill_detail(
        request: Request,
        skill_id: str,
        msg: str = "",
        msg_type: str = "success",
    ):
        client = get_client()
        try:
            skill = await client.get_skill(skill_id)
            versions = await client.list_skill_versions(skill_id)
            error = None
        except MCPError as exc:
            return _redirect("/skills", str(exc), "error")
        return _render(request, "skill.html", {
            "active": "skills",
            "skill": skill,
            "versions": versions,
            "metadata_json": json.dumps(skill.get("metadata") or {}, indent=2),
            "error": error,
            **_flash_ctx(msg, msg_type),
        })

    @app.post("/skills/{skill_id}/update")
    async def update_skill(
        skill_id: str,
        name: Annotated[str, Form()],
        version: Annotated[str, Form()],
        description: Annotated[str, Form()] = "",
        metadata: Annotated[str, Form()] = "{}",
    ):
        try:
            meta = json.loads(metadata)
        except json.JSONDecodeError:
            return _redirect(f"/skills/{skill_id}", "metadata must be valid JSON.", "error")
        try:
            await get_client().update_skill(
                skill_id,
                {"name": name, "description": description, "version": version, "metadata": meta},
            )
            return _redirect(f"/skills/{skill_id}", "Skill updated.")
        except MCPError as exc:
            return _redirect(f"/skills/{skill_id}", str(exc), "error")

    @app.post("/skills/{skill_id}/versions")
    async def create_skill_version(
        skill_id: str,
        version: Annotated[str, Form()],
        name: Annotated[str, Form()],
        description: Annotated[str, Form()] = "",
        metadata: Annotated[str, Form()] = "{}",
    ):
        try:
            meta = json.loads(metadata)
        except json.JSONDecodeError:
            return _redirect(f"/skills/{skill_id}", "metadata must be valid JSON.", "error")
        try:
            await get_client().create_skill({
                "id": skill_id, "name": name, "description": description,
                "version": version, "metadata": meta, "skillset_ids": [],
            })
            return _redirect(f"/skills/{skill_id}", f"Version {version} added.")
        except MCPError as exc:
            return _redirect(f"/skills/{skill_id}", str(exc), "error")

    @app.delete("/skills/{skill_id}")
    async def delete_skill(skill_id: str):
        try:
            await get_client().delete_skill(skill_id)
            return Response(status_code=200)
        except MCPError:
            return Response(status_code=500)

    @app.delete("/skills/{skill_id}/versions/{version:path}")
    async def delete_skill_version(skill_id: str, version: str):
        try:
            await get_client().delete_skill(skill_id, version=version)
            return Response(status_code=200)
        except MCPError:
            return Response(status_code=500)

    return app


def run() -> None:
    settings = get_settings()
    uvicorn.run(
        "webui.main:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        reload=True,
    )
