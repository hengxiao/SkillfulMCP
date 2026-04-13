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
from fastapi import Depends, FastAPI, File, Form, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .auth import (
    Operator,
    authenticate,
    clear_session,
    get_csrf_token,
    get_session_operator,
    set_session_operator,
)
from .client import MCPClient, MCPError
from .config import get_settings
from .middleware import AuthMiddleware, csrf_required


# Shorthand so every mutating route can declare `dependencies=CSRF` and
# stay readable. Each POST/PUT/DELETE handler below uses it.
CSRF: list = [Depends(csrf_required)]

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
    if not msg:
        return RedirectResponse(path, status_code=303)
    # Preserve any existing query string in `path` instead of blindly appending
    # "?msg=...", which would produce an illegal double-'?' URL like
    # "/skills/pdf?version=1.0.0?msg=..." and confuse the next handler.
    sep = "&" if "?" in path else "?"
    url = f"{path}{sep}msg={quote(msg)}&msg_type={msg_type}"
    return RedirectResponse(url, status_code=303)


def _render(request: Request, template: str, ctx: dict) -> HTMLResponse:
    # Session-bound CSRF token + current operator are available in every
    # template without each handler having to remember. Handlers can still
    # override via `ctx` if they really need to.
    ctx.setdefault("csrf_token", get_csrf_token(request))
    ctx.setdefault("operator", get_session_operator(request))
    return templates.TemplateResponse(request, template, ctx)


def _flash_ctx(msg: str, msg_type: str) -> dict:
    return {"msg": msg, "msg_type": msg_type}


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app() -> FastAPI:
    app = FastAPI(title="SkillfulMCP Web UI", docs_url=None, redoc_url=None)

    # ------------------------------------------------------------------ #
    # Middleware stack                                                    #
    # ------------------------------------------------------------------ #
    # `add_middleware` prepends to the stack, so the last-added is the
    # outermost and runs first on ingress. Order on the way in:
    #   SessionMiddleware → AuthMiddleware → CSRFMiddleware → handler
    settings = get_settings()
    if not settings.session_secret:
        raise RuntimeError(
            "MCP_WEBUI_SESSION_SECRET must be set. Generate one with: "
            "python -c 'import secrets; print(secrets.token_urlsafe(32))'"
        )

    # CSRF is NOT a middleware — it's a FastAPI dep applied per route (see
    # `CSRF` list at module top). BaseHTTPMiddleware can't read the body
    # without breaking downstream `Form()` deps.
    app.add_middleware(AuthMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        same_site="lax",
        https_only=False,  # flipped by ops behind an HTTPS reverse proxy
    )

    # ------------------------------------------------------------------ #
    # Login / logout                                                      #
    # ------------------------------------------------------------------ #

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, next: str = "/", error: str = ""):
        # Pre-populate the CSRF token so the login form has a valid one
        # even without a prior session.
        return _render(request, "login.html", {
            "error": error,
            "next": next,
        })

    @app.post("/login")
    async def login_submit(
        request: Request,
        email: Annotated[str, Form()],
        password: Annotated[str, Form()],
        csrf_token: Annotated[str, Form()] = "",
        next: Annotated[str, Form()] = "/",
    ):
        # CSRF on /login is enforced here (not in the middleware — see
        # CSRFMiddleware docstring for why it's exempt).
        from .auth import verify_csrf
        if settings.csrf_enabled and not verify_csrf(request, csrf_token):
            return _render(request, "login.html", {
                "error": "Your login form expired. Please try again.",
                "next": next,
            })

        op = authenticate(email, password)
        if op is None:
            return _render(request, "login.html", {
                "error": "Invalid email or password.",
                "next": next,
            })
        set_session_operator(request, op)
        # Only redirect to internal paths to avoid open-redirect abuse.
        safe_next = next if next.startswith("/") and not next.startswith("//") else "/"
        return RedirectResponse(safe_next, status_code=303)

    @app.post("/logout", dependencies=CSRF)
    async def logout(request: Request):
        clear_session(request)
        return RedirectResponse("/login", status_code=303)

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

    @app.get("/skillsets/{skillset_id}/modal", response_class=HTMLResponse)
    async def skillset_modal(request: Request, skillset_id: str):
        """Return the modal body partial for a quick-view of a skillset."""
        client = get_client()
        try:
            skillset = await client.get_skillset(skillset_id)
            member_skills = await client.list_skillset_skills(skillset_id)
        except MCPError as exc:
            return HTMLResponse(
                f'<div class="modal-body"><div class="alert alert-danger mb-0">'
                f'{exc}</div></div>',
                status_code=exc.status_code or 500,
            )
        return _render(request, "_skillset_modal.html", {
            "skillset": skillset,
            "member_skills": member_skills,
        })

    @app.post("/skillsets", dependencies=CSRF)
    async def create_skillset(
        id: Annotated[str, Form()],
        name: Annotated[str, Form()],
        description: Annotated[str, Form()] = "",
        visibility: Annotated[str, Form()] = "private",
    ):
        try:
            await get_client().create_skillset({
                "id": id, "name": name, "description": description,
                "visibility": visibility,
            })
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

    @app.post("/skillsets/{skillset_id}/update", dependencies=CSRF)
    async def update_skillset(
        skillset_id: str,
        name: Annotated[str, Form()],
        description: Annotated[str, Form()] = "",
        visibility: Annotated[str, Form()] = "private",
    ):
        try:
            await get_client().update_skillset(skillset_id, {
                "id": skillset_id, "name": name, "description": description,
                "visibility": visibility,
            })
            return _redirect(f"/skillsets/{skillset_id}", "Skillset updated.")
        except MCPError as exc:
            return _redirect(f"/skillsets/{skillset_id}", str(exc), "error")

    @app.delete("/skillsets/{skillset_id}", dependencies=CSRF)
    async def delete_skillset(skillset_id: str):
        try:
            await get_client().delete_skillset(skillset_id)
            return Response(status_code=200)
        except MCPError:
            return Response(status_code=500)

    @app.post("/skillsets/{skillset_id}/skills", dependencies=CSRF)
    async def associate_skill(
        skillset_id: str,
        skill_id: Annotated[str, Form()],
    ):
        try:
            await get_client().associate_skill(skillset_id, skill_id)
            return _redirect(f"/skillsets/{skillset_id}", f"Skill '{skill_id}' added.")
        except MCPError as exc:
            return _redirect(f"/skillsets/{skillset_id}", str(exc), "error")

    @app.delete("/skillsets/{skillset_id}/skills/{skill_id}", dependencies=CSRF)
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
        skill_membership: dict[str, list[str]] = {}
        try:
            skills = await client.list_skills()
            skillsets = await client.list_skillsets()
            # Build skill_id -> [skillset_ids] so the UI can filter by skillset.
            for ss in skillsets:
                try:
                    members = await client.list_skillset_skills(ss["id"])
                except MCPError:
                    members = []
                for m in members:
                    skill_membership.setdefault(m["id"], []).append(ss["id"])
            error = None
        except MCPError as exc:
            skills, skillsets, error = [], [], str(exc)
        return _render(request, "skills.html", {
            "active": "skills",
            "skills": skills,
            "skillsets": skillsets,
            "skill_membership": skill_membership,
            "error": error,
            **_flash_ctx(msg, msg_type),
        })

    @app.get("/skills/{skill_id}/modal", response_class=HTMLResponse)
    async def skill_modal(request: Request, skill_id: str):
        """Return the modal body partial for a quick-view of a skill."""
        client = get_client()
        try:
            skill = await client.get_skill(skill_id)
            versions = await client.list_skill_versions(skill_id)
            try:
                bundle_files = await client.list_bundle_files(
                    skill_id, skill["version"]
                )
            except MCPError:
                bundle_files = []
        except MCPError as exc:
            return HTMLResponse(
                f'<div class="modal-body"><div class="alert alert-danger mb-0">'
                f'{exc}</div></div>',
                status_code=exc.status_code or 500,
            )
        return _render(request, "_skill_modal.html", {
            "skill": skill,
            "versions": versions,
            "bundle_files": bundle_files,
            "metadata_json": json.dumps(skill.get("metadata") or {}, indent=2),
        })

    @app.post("/skills", dependencies=CSRF)
    async def create_skill(
        id: Annotated[str, Form()],
        name: Annotated[str, Form()],
        version: Annotated[str, Form()],
        description: Annotated[str, Form()] = "",
        metadata: Annotated[str, Form()] = "{}",
        skillset_ids: Annotated[list[str], Form()] = [],
        visibility: Annotated[str, Form()] = "private",
    ):
        try:
            meta = json.loads(metadata)
        except json.JSONDecodeError:
            return _redirect("/skills", "metadata must be valid JSON.", "error")
        try:
            await get_client().create_skill({
                "id": id, "name": name, "description": description,
                "version": version, "metadata": meta,
                "skillset_ids": skillset_ids,
                "visibility": visibility,
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
        version: str | None = None,
        msg: str = "",
        msg_type: str = "success",
    ):
        """Skill detail. ?version=X.Y.Z selects a specific version (default: latest)."""
        client = get_client()
        try:
            # Load the requested (or latest) version + the full version list.
            skill = await client.get_skill(skill_id, version=version)
            versions = await client.list_skill_versions(skill_id)
            # Bundle for the version we're displaying.
            try:
                bundle_files = await client.list_bundle_files(
                    skill_id, skill["version"]
                )
            except MCPError:
                bundle_files = []
            # Render SKILL.md inline if present.
            skill_md = None
            for bf in bundle_files:
                if bf["path"].lower() == "skill.md":
                    try:
                        content = await client.get_bundle_file(
                            skill_id, skill["version"], bf["path"]
                        )
                        skill_md = content.decode("utf-8", errors="replace")
                    except MCPError:
                        pass
                    break
            error = None
        except MCPError as exc:
            return _redirect("/skills", str(exc), "error")
        return _render(request, "skill.html", {
            "active": "skills",
            "skill": skill,
            "versions": versions,
            "bundle_files": bundle_files,
            "skill_md": skill_md,
            "metadata_json": json.dumps(skill.get("metadata") or {}, indent=2),
            "error": error,
            **_flash_ctx(msg, msg_type),
        })

    # ------------------------------------------------------------------ #
    # Clone skill — create a brand-new skill id, prefilled from a source  #
    # version. Used when a user wants to rename a skill (names are        #
    # immutable within a skill id).                                       #
    # ------------------------------------------------------------------ #

    @app.get("/skills/{skill_id}/clone", response_class=HTMLResponse)
    async def clone_page(
        request: Request,
        skill_id: str,
        from_: str | None = None,
    ):
        from_ = request.query_params.get("from") or from_
        client = get_client()
        try:
            source = await client.get_skill(skill_id, version=from_)
            try:
                src_bundle_files = await client.list_bundle_files(
                    skill_id, source["version"]
                )
            except MCPError:
                src_bundle_files = []
        except MCPError as exc:
            return _redirect("/skills", str(exc), "error")
        return _render(request, "skill_clone.html", {
            "active": "skills",
            "source": source,
            "metadata_json": json.dumps(source.get("metadata") or {}, indent=2),
            "src_bundle_file_count": len(src_bundle_files),
            "error": None,
            **_flash_ctx("", "success"),
        })

    @app.post("/skills/{skill_id}/clone", dependencies=CSRF)
    async def clone_skill(
        skill_id: str,
        new_id: Annotated[str, Form()],
        new_name: Annotated[str, Form()],
        version: Annotated[str, Form()],
        from_version: Annotated[str, Form()],
        bundle_action: Annotated[str, Form()],
        description: Annotated[str, Form()] = "",
        metadata: Annotated[str, Form()] = "{}",
        visibility: Annotated[str, Form()] = "private",
        file: UploadFile | None = File(default=None),
    ):
        back = f"/skills/{skill_id}/clone?from={from_version}"
        try:
            meta = json.loads(metadata)
        except json.JSONDecodeError:
            return _redirect(back, "metadata must be valid JSON.", "error")

        client = get_client()
        # Create the new skill row.
        try:
            await client.create_skill({
                "id": new_id,
                "name": new_name,
                "description": description,
                "version": version,
                "metadata": meta,
                "skillset_ids": [],
                "visibility": visibility,
            })
        except MCPError as exc:
            return _redirect(back, str(exc), "error")

        dest = f"/skills/{new_id}?version={version}"

        if bundle_action == "copy":
            try:
                await client.copy_bundle(new_id, version, skill_id, from_version)
                return _redirect(
                    dest,
                    f"Cloned {skill_id} → {new_id} (v{version}); "
                    f"bundle copied from v{from_version}.",
                )
            except MCPError as exc:
                return _redirect(
                    dest,
                    f"Cloned but bundle copy failed: {exc}",
                    "error",
                )
        if bundle_action == "upload":
            if file is None or not file.filename:
                return _redirect(
                    dest,
                    f"Cloned to {new_id}, but no bundle file was uploaded.",
                    "error",
                )
            try:
                data = await file.read()
                await client.upload_bundle(new_id, version, file.filename, data)
                return _redirect(
                    dest, f"Cloned to {new_id} (v{version}) with new bundle."
                )
            except MCPError as exc:
                return _redirect(
                    dest,
                    f"Cloned but bundle upload failed: {exc}",
                    "error",
                )
        # 'none'
        return _redirect(dest, f"Cloned to {new_id} (no bundle).")

    # ------------------------------------------------------------------ #
    # New version — prefilled from an existing version, may inherit the   #
    # source bundle or upload a new one. The view page is read-only; all  #
    # metadata/bundle changes happen here and produce a new version.      #
    # ------------------------------------------------------------------ #

    @app.get("/skills/{skill_id}/new-version", response_class=HTMLResponse)
    async def new_version_page(
        request: Request,
        skill_id: str,
        from_: str | None = None,
    ):
        # Accept ?from= via alias to avoid colliding with Python's keyword.
        from_ = request.query_params.get("from") or from_
        client = get_client()
        try:
            source = await client.get_skill(skill_id, version=from_)
            try:
                src_bundle_files = await client.list_bundle_files(
                    skill_id, source["version"]
                )
            except MCPError:
                src_bundle_files = []
        except MCPError as exc:
            return _redirect("/skills", str(exc), "error")
        return _render(request, "skill_new_version.html", {
            "active": "skills",
            "source": source,
            "metadata_json": json.dumps(source.get("metadata") or {}, indent=2),
            "src_bundle_file_count": len(src_bundle_files),
            "error": None,
            **_flash_ctx("", "success"),
        })

    @app.post("/skills/{skill_id}/new-version", dependencies=CSRF)
    async def create_new_version(
        skill_id: str,
        version: Annotated[str, Form()],
        from_version: Annotated[str, Form()],
        bundle_action: Annotated[str, Form()],  # 'copy' | 'upload' | 'none'
        description: Annotated[str, Form()] = "",
        metadata: Annotated[str, Form()] = "{}",
        visibility: Annotated[str, Form()] = "private",
        file: UploadFile | None = File(default=None),
    ):
        back = f"/skills/{skill_id}/new-version?from={from_version}"
        try:
            meta = json.loads(metadata)
        except json.JSONDecodeError:
            return _redirect(back, "metadata must be valid JSON.", "error")

        client = get_client()
        # Name is immutable for a skill — inherit from the source version.
        try:
            source = await client.get_skill(skill_id, version=from_version)
        except MCPError as exc:
            return _redirect(back, str(exc), "error")

        # 1) Create the new version row.
        try:
            await client.create_skill({
                "id": skill_id,
                "name": source["name"],
                "description": description,
                "version": version,
                "metadata": meta,
                "skillset_ids": [],
                "visibility": visibility,
            })
        except MCPError as exc:
            return _redirect(back, str(exc), "error")

        dest = f"/skills/{skill_id}?version={version}"

        # 2) Attach the bundle per the user's choice.
        if bundle_action == "copy":
            try:
                await client.copy_bundle(skill_id, version, skill_id, from_version)
                return _redirect(
                    dest,
                    f"Version {version} created, bundle copied from v{from_version}.",
                )
            except MCPError as exc:
                return _redirect(
                    dest,
                    f"Version {version} created, but bundle copy failed: {exc}",
                    "error",
                )
        if bundle_action == "upload":
            if file is None or not file.filename:
                return _redirect(
                    dest,
                    f"Version {version} created, but no bundle file was uploaded.",
                    "error",
                )
            try:
                data = await file.read()
                await client.upload_bundle(
                    skill_id, version, file.filename, data
                )
                return _redirect(dest, f"Version {version} created with new bundle.")
            except MCPError as exc:
                return _redirect(
                    dest,
                    f"Version {version} created, but bundle upload failed: {exc}",
                    "error",
                )
        # 'none' — leave the new version bundle-less.
        return _redirect(dest, f"Version {version} created (no bundle).")

    @app.delete("/skills/{skill_id}", dependencies=CSRF)
    async def delete_skill(skill_id: str):
        try:
            await get_client().delete_skill(skill_id)
            return Response(status_code=200)
        except MCPError:
            return Response(status_code=500)

    @app.delete("/skills/{skill_id}/versions/{version:path}", dependencies=CSRF)
    async def delete_skill_version(skill_id: str, version: str):
        try:
            await get_client().delete_skill(skill_id, version=version)
            return Response(status_code=200)
        except MCPError:
            return Response(status_code=500)

    # ------------------------------------------------------------------ #
    # Bundle file fetch (for the viewer modal)                            #
    # ------------------------------------------------------------------ #

    @app.get("/skills/{skill_id}/versions/{version}/files/{path:path}")
    async def download_bundle_file(skill_id: str, version: str, path: str):
        try:
            data = await get_client().get_bundle_file(skill_id, version, path)
            return Response(content=data, media_type="application/octet-stream")
        except MCPError as exc:
            return Response(content=str(exc), status_code=exc.status_code or 500)

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
