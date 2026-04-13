"""
SkillfulMCP Web UI — FastAPI application.

All data flows through MCPClient, which proxies to the MCP server API
with the configured admin key. Forms use standard POST + redirect
(PRG) for create/update operations. Deletes use HTMX so rows are
removed inline without a full page reload. Flash messages are passed
as `?msg=...&msg_type=success|error` query params.

Route map (see spec/delivery.md §4 for the authoritative list):

- `/` — public landing, exempt from AuthMiddleware; shows
  public-visibility skills + skillsets to anonymous visitors and a
  dashboard-style counts row to logged-in users.
- `/login`, `/logout` — session-cookie auth; CSRF-protected.
- `/skillsets`, `/skillsets/{id}[/modal]` + mutations — catalog
  management + quick-view modal.
- `/skills`, `/skills/{id}[/modal]`, `/skills/{id}/clone`,
  `/skills/{id}/new-version` — immutable-version skill workflow +
  clone-to-rename.
- `/skills/{id}/versions/{ver}/files/{path:path}` — bundle file
  download proxy.
- `/agents`, `/agents/{id}/tokens/new`, `/agents/{id}/tokens` —
  agent listing + Wave 8c mint-token wizard.
- `/users`, `/users/new`, `/users/{id}`, `/account` — Wave 8b
  operator CRUD; role surfaces were removed in Wave 9.0 (account
  memberships take over in Wave 9.5).
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

import uvicorn
from fastapi import Depends, FastAPI, File, Form, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .auth import (
    Operator,
    authenticate,
    authenticate_via_server,
    clear_session,
    get_csrf_token,
    get_session_operator,
    set_session_operator,
)
from .client import MCPClient, MCPError
from .config import get_settings
from .middleware import AuthMiddleware, csrf_required, require_role
from . import oidc as oidc_mod


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
            "oidc_enabled": oidc_mod.is_enabled(),
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

        # Wave 8b: try DB-backed authentication first (via the mcp_server
        # /admin/users/authenticate endpoint). Fall back to the env-only
        # list for disaster-recovery when the server is unreachable or
        # the table is empty.
        op = await authenticate_via_server(email, password)
        if op is None:
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
    # OIDC login (item G)                                                  #
    # ------------------------------------------------------------------ #

    @app.get("/auth/oidc/login")
    async def oidc_login(request: Request):
        cfg = oidc_mod.OIDCConfig.from_env()
        if cfg is None:
            return _redirect("/login", "OIDC is not configured.", "error")
        state, nonce = oidc_mod.fresh_state()
        oidc_mod.stash_state(request, state, nonce)
        try:
            url = oidc_mod.build_login_url(cfg, state=state, nonce=nonce)
        except Exception as exc:
            return _redirect("/login", f"OIDC discovery failed: {exc}", "error")
        return RedirectResponse(url, status_code=303)

    @app.get("/auth/oidc/callback")
    async def oidc_callback(
        request: Request,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ):
        cfg = oidc_mod.OIDCConfig.from_env()
        if cfg is None:
            return _redirect("/login", "OIDC is not configured.", "error")
        if error or not code:
            return _redirect(
                "/login",
                f"OIDC login failed: {error or 'no code'}",
                "error",
            )
        expected_state, nonce = oidc_mod.pop_state(request)
        if not expected_state or state != expected_state:
            return _redirect(
                "/login",
                "OIDC state mismatch — try again.",
                "error",
            )
        try:
            claims = oidc_mod.exchange_and_verify(
                cfg, code=code, nonce=nonce or "",
            )
        except oidc_mod.OIDCError as exc:
            return _redirect(
                "/login", f"OIDC login failed: {exc}", "error"
            )

        email = (claims.get("email") or "").strip().lower()
        if not email:
            return _redirect(
                "/login", "OIDC id_token missing email claim.", "error",
            )

        # Ask the catalog to resolve-or-create the user. Signup
        # endpoint handles either case: if the email exists we get
        # 409, which we translate into an authenticate-and-continue
        # path. If new, we create via signup with a long random
        # placeholder password (the user can never log in with it —
        # OIDC is their only path).
        client = get_client()
        user_id: str | None = None
        try:
            user = await client.signup(
                email=email,
                password=secrets.token_urlsafe(32),
                display_name=claims.get("name") or None,
            )
            user_id = user.get("id")
        except MCPError as exc:
            if exc.status_code != 409:
                return _redirect(
                    "/login", f"OIDC login failed: {exc}", "error",
                )
            # Existing user — fine, we just attach the session.
            user_id = None  # Resolved later if the UI needs it.

        op = Operator(
            email=email,
            role="admin",
            user_id=user_id,
            is_superadmin=False,
            active_account_id=None,
        )
        set_session_operator(request, op)
        return RedirectResponse("/", status_code=303)

    # ------------------------------------------------------------------ #
    # Dashboard                                                           #
    # ------------------------------------------------------------------ #

    @app.get("/", response_class=HTMLResponse)
    async def landing(request: Request, msg: str = "", msg_type: str = "success"):
        """Public landing page.

        Anonymous visitors see only public skills + skillsets — it's a
        read-only browse of the open catalog. Logged-in operators see
        the same list plus counts across everything (as before).
        """
        client = get_client()
        op = get_session_operator(request)
        try:
            all_skills = await client.list_skills()
            all_skillsets = await client.list_skillsets()
            error = None
        except MCPError as exc:
            all_skills, all_skillsets, error = [], [], str(exc)

        public_skills = [s for s in all_skills if s.get("visibility") == "public"]
        public_skillsets = [s for s in all_skillsets if s.get("visibility") == "public"]

        agents = []
        if op is not None:
            try:
                agents = await client.list_agents()
            except MCPError:
                agents = []

        return _render(request, "landing.html", {
            "active": "dashboard",
            "public_skills": public_skills,
            "public_skillsets": public_skillsets,
            "skillsets_count": len(all_skillsets),
            "skills_count": len(all_skills),
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

    # ------------------------------------------------------------------ #
    # Accounts (Wave 9.5)                                                 #
    # ------------------------------------------------------------------ #

    async def _memberships_for(op: Operator | None) -> list[dict]:
        """Fetch the caller's membership rows from every account they
        belong to. Returns an empty list for unauth'd / env-fallback /
        superadmin sessions (superadmins are NOT members of any
        account; they see all accounts via the same /admin/accounts
        list surface).

        Re-fetched per request (spec §3.3 — no session cache), so
        role changes elsewhere propagate on the next click."""
        if op is None or op.is_superadmin or not op.user_id:
            return []
        try:
            accounts = await get_client().list_accounts()
        except MCPError:
            return []
        # Check membership in each. N*1 queries; accounts are typically
        # small (<20 per operator) so cost is fine.
        mine: list[dict] = []
        for a in accounts:
            try:
                members = await get_client().list_members(a["id"])
            except MCPError:
                continue
            for m in members:
                if m.get("pending"):
                    continue
                if m.get("user_id") == op.user_id:
                    mine.append({
                        "account_id": a["id"],
                        "account_name": a["name"],
                        "role": m.get("role", "viewer"),
                    })
                    break
        return mine

    async def _ensure_active_account(request: Request) -> Operator | None:
        """Stamp active_account_id on first request after login.

        Picks the caller's first membership. If they have none, leaves
        active_account_id as None and the Web UI renders a "no
        accounts yet" banner.
        """
        op = get_session_operator(request)
        if op is None or op.is_superadmin:
            return op
        if op.active_account_id:
            return op
        mems = await _memberships_for(op)
        if mems:
            new_op = Operator(
                email=op.email,
                role=op.role,
                user_id=op.user_id,
                is_superadmin=op.is_superadmin,
                active_account_id=mems[0]["account_id"],
            )
            set_session_operator(request, new_op)
            return new_op
        return op

    @app.get("/accounts", response_class=HTMLResponse)
    async def accounts_page(request: Request, msg: str = "", msg_type: str = "success"):
        op = get_session_operator(request)
        if op is None:
            return _redirect("/login", "Sign in to view accounts.", "error")
        # Superadmin sees every account; regular users see only
        # accounts they're a member of.
        try:
            if op.is_superadmin:
                accounts = await get_client().list_accounts()
                my_role_by_account: dict[str, str] = {}
            else:
                mems = await _memberships_for(op)
                my_role_by_account = {m["account_id"]: m["role"] for m in mems}
                # Fetch full rows for the accounts they're in.
                all_accounts = await get_client().list_accounts()
                accounts = [a for a in all_accounts if a["id"] in my_role_by_account]
            error = None
        except MCPError as exc:
            accounts, my_role_by_account, error = [], {}, str(exc)
        return _render(request, "accounts.html", {
            "active": "accounts",
            "accounts": accounts,
            "my_role_by_account": my_role_by_account,
            "error": error,
            **_flash_ctx(msg, msg_type),
        })

    @app.get("/accounts/new", response_class=HTMLResponse)
    async def account_new_page(request: Request):
        op = get_session_operator(request)
        if op is None:
            return _redirect("/login", "Sign in first.", "error")
        if op.is_superadmin:
            # Superadmin has no users.id to anchor the first
            # membership to — they'd either need an existing user to
            # promote, or this flow is inappropriate. Redirect back
            # to /accounts with an explanation.
            return _redirect(
                "/accounts",
                "Superadmin cannot create accounts; log in as a regular user.",
                "error",
            )
        return _render(request, "account_new.html", {"active": "accounts"})

    @app.post("/accounts", dependencies=CSRF)
    async def create_account(
        request: Request,
        name: Annotated[str, Form()],
    ):
        op = get_session_operator(request)
        if op is None or op.is_superadmin or not op.user_id:
            return _redirect("/accounts", "Sign in as a regular user first.", "error")
        try:
            acct = await get_client().create_account(
                name=name, initial_admin_user_id=op.user_id
            )
        except MCPError as exc:
            return _redirect("/accounts/new", str(exc), "error")
        # Stamp the new account as active so the next page load
        # lands the user inside it.
        new_op = Operator(
            email=op.email, role=op.role, user_id=op.user_id,
            is_superadmin=op.is_superadmin, active_account_id=acct["id"],
        )
        set_session_operator(request, new_op)
        return _redirect(
            f"/accounts/{acct['id']}", f"Account '{acct['name']}' created."
        )

    @app.get("/accounts/{account_id}", response_class=HTMLResponse)
    async def account_detail_page(
        request: Request, account_id: str,
        msg: str = "", msg_type: str = "success",
    ):
        op = get_session_operator(request)
        if op is None:
            return _redirect("/login", "Sign in first.", "error")
        try:
            acct = await get_client().get_account(account_id)
            members = await get_client().list_members(account_id)
            error = None
        except MCPError as exc:
            return _redirect("/accounts", str(exc), "error")
        # Caller's own role in this account — drives which controls render.
        mems = await _memberships_for(op)
        my_role = next(
            (m["role"] for m in mems if m["account_id"] == account_id),
            None,
        )
        can_manage = op.is_superadmin or my_role == "account-admin"
        return _render(request, "account_detail.html", {
            "active": "accounts",
            "account": acct,
            "members": members,
            "my_role": my_role,
            "can_manage": can_manage,
            "error": error,
            **_flash_ctx(msg, msg_type),
        })

    @app.post("/accounts/{account_id}/members", dependencies=CSRF)
    async def invite_member(
        request: Request,
        account_id: str,
        email: Annotated[str, Form()],
        role: Annotated[str, Form()],
    ):
        try:
            await get_client().invite_member(account_id, email=email, role=role)
        except MCPError as exc:
            return _redirect(f"/accounts/{account_id}", str(exc), "error")
        return _redirect(f"/accounts/{account_id}", f"Invited {email} as {role}.")

    @app.post("/accounts/{account_id}/members/{user_id}/role",
              dependencies=CSRF)
    async def update_member_role(
        request: Request,
        account_id: str, user_id: str,
        role: Annotated[str, Form()],
    ):
        try:
            await get_client().update_member_role(account_id, user_id, role)
        except MCPError as exc:
            return _redirect(f"/accounts/{account_id}", str(exc), "error")
        return _redirect(f"/accounts/{account_id}", "Role updated.")

    @app.delete("/accounts/{account_id}/members/{user_id}",
                dependencies=CSRF)
    async def remove_member(
        request: Request, account_id: str, user_id: str,
    ):
        try:
            await get_client().remove_member(account_id, user_id)
            return Response(status_code=200)
        except MCPError as exc:
            return Response(content=exc.detail, status_code=exc.status_code or 500)

    @app.delete("/accounts/{account_id}/pending/{pending_id}",
                dependencies=CSRF)
    async def revoke_pending(
        request: Request, account_id: str, pending_id: int,
    ):
        try:
            await get_client().delete_pending_invite(account_id, pending_id)
            return Response(status_code=200)
        except MCPError as exc:
            return Response(content=exc.detail, status_code=exc.status_code or 500)

    @app.post("/session/switch-account", dependencies=CSRF)
    async def switch_account(
        request: Request,
        account_id: Annotated[str, Form()],
        next: Annotated[str, Form()] = "/",
    ):
        op = get_session_operator(request)
        if op is None:
            return _redirect("/login", "Sign in first.", "error")
        # Verify the caller is a member of the requested account
        # (superadmin bypass stays).
        if not op.is_superadmin:
            mems = await _memberships_for(op)
            if not any(m["account_id"] == account_id for m in mems):
                return _redirect(
                    "/accounts",
                    "You are not a member of that account.",
                    "error",
                )
        new_op = Operator(
            email=op.email, role=op.role, user_id=op.user_id,
            is_superadmin=op.is_superadmin, active_account_id=account_id,
        )
        set_session_operator(request, new_op)
        safe_next = next if next.startswith("/") and not next.startswith("//") else "/"
        return RedirectResponse(safe_next, status_code=303)

    # ------------------------------------------------------------------ #
    # Agents + token issuance (Wave 8c)                                   #
    # ------------------------------------------------------------------ #

    @app.get("/agents", response_class=HTMLResponse)
    async def agents_page(request: Request, msg: str = "", msg_type: str = "success"):
        try:
            agents = await get_client().list_agents()
            error = None
        except MCPError as exc:
            agents, error = [], str(exc)
        return _render(request, "agents.html", {
            "active": "agents",
            "agents": agents,
            "error": error,
            **_flash_ctx(msg, msg_type),
        })

    @app.get("/agents/{agent_id}/tokens/new", response_class=HTMLResponse,
             dependencies=[Depends(require_role("admin"))])
    async def token_mint_form(request: Request, agent_id: str):
        try:
            agent = await get_client().get_agent(agent_id)
        except MCPError as exc:
            return _redirect("/agents", str(exc), "error")
        return _render(request, "token_new.html", {
            "active": "agents",
            "agent": agent,
            "error": None,
            **_flash_ctx("", "success"),
        })

    @app.post("/agents/{agent_id}/tokens",
              dependencies=CSRF + [Depends(require_role("admin"))])
    async def token_mint_submit(
        request: Request,
        agent_id: str,
        expires_in: Annotated[int, Form()] = 3600,
        skills: Annotated[list[str], Form()] = [],
        skillsets: Annotated[list[str], Form()] = [],
        scope: Annotated[list[str], Form()] = [],
    ):
        """Mint a narrowed token. Empty list means "inherit all agent
        grants"; any checkbox selection narrows to just those. The
        resulting token is rendered once — never stored anywhere."""
        payload: dict = {"agent_id": agent_id, "expires_in": expires_in}
        # An empty list from the form means "leave field default"; we
        # distinguish by also inspecting the raw form so "no checkboxes"
        # defaults to the agent's full grants instead of stripping them.
        form = await request.form()
        if "_skills_present" in form:
            payload["skills"] = skills
        if "_skillsets_present" in form:
            payload["skillsets"] = skillsets
        if "_scope_present" in form:
            payload["scope"] = scope
        try:
            tok = await get_client().issue_token(payload)
        except MCPError as exc:
            return _redirect(f"/agents/{agent_id}/tokens/new", str(exc), "error")
        try:
            agent = await get_client().get_agent(agent_id)
        except MCPError:
            agent = {"id": agent_id, "name": agent_id}
        return _render(request, "token_result.html", {
            "active": "agents",
            "agent": agent,
            "access_token": tok["access_token"],
            "expires_in": tok["expires_in"],
            "issued_skills": payload.get("skills"),
            "issued_skillsets": payload.get("skillsets"),
            "issued_scope": payload.get("scope"),
        })

    # ------------------------------------------------------------------ #
    # User management (admin only) + self-service /account                #
    # ------------------------------------------------------------------ #

    @app.get("/users", response_class=HTMLResponse,
             dependencies=[Depends(require_role("admin"))])
    async def users_page(request: Request, msg: str = "", msg_type: str = "success"):
        try:
            users = await get_client().list_users()
            error = None
        except MCPError as exc:
            users, error = [], str(exc)
        return _render(request, "users.html", {
            "active": "users",
            "users": users,
            "error": error,
            **_flash_ctx(msg, msg_type),
        })

    @app.get("/users/new", response_class=HTMLResponse,
             dependencies=[Depends(require_role("admin"))])
    async def users_new_page(request: Request):
        return _render(request, "user_new.html", {"active": "users"})

    @app.post("/users", dependencies=CSRF + [Depends(require_role("admin"))])
    async def create_user(
        email: Annotated[str, Form()],
        password: Annotated[str, Form()],
        display_name: Annotated[str, Form()] = "",
    ):
        # Wave 9 drops the platform role from the admin-users flow;
        # account-scoped roles live on memberships and will get their
        # own UI in Wave 9.5.
        try:
            await get_client().create_user({
                "email": email,
                "password": password,
                "display_name": display_name or None,
            })
            return _redirect("/users", f"User '{email}' created.")
        except MCPError as exc:
            return _redirect("/users", str(exc), "error")

    @app.get("/users/{user_id}", response_class=HTMLResponse,
             dependencies=[Depends(require_role("admin"))])
    async def user_detail(request: Request, user_id: str,
                          msg: str = "", msg_type: str = "success"):
        try:
            user = await get_client().get_user(user_id)
        except MCPError as exc:
            return _redirect("/users", str(exc), "error")
        return _render(request, "user_detail.html", {
            "active": "users",
            "user": user,
            **_flash_ctx(msg, msg_type),
        })

    @app.post("/users/{user_id}/update",
              dependencies=CSRF + [Depends(require_role("admin"))])
    async def update_user(
        user_id: str,
        disabled: Annotated[str, Form()] = "",
        display_name: Annotated[str, Form()] = "",
        password: Annotated[str, Form()] = "",
    ):
        body: dict = {
            "disabled": disabled == "on",
            "display_name": display_name or None,
        }
        if password:
            body["password"] = password
        try:
            await get_client().update_user(user_id, body)
            return _redirect(f"/users/{user_id}", "User updated.")
        except MCPError as exc:
            return _redirect(f"/users/{user_id}", str(exc), "error")

    @app.delete("/users/{user_id}",
                dependencies=CSRF + [Depends(require_role("admin"))])
    async def delete_user_route(user_id: str):
        try:
            await get_client().delete_user(user_id)
            return Response(status_code=200)
        except MCPError as exc:
            return Response(content=exc.detail, status_code=exc.status_code or 500)

    @app.get("/account", response_class=HTMLResponse)
    async def account_page(request: Request, msg: str = "", msg_type: str = "success"):
        op = get_session_operator(request)
        return _render(request, "account.html", {
            "active": "account",
            "operator": op,
            **_flash_ctx(msg, msg_type),
        })

    @app.post("/account/password", dependencies=CSRF)
    async def account_change_password(
        request: Request,
        new_password: Annotated[str, Form()],
        confirm_password: Annotated[str, Form()],
    ):
        op = get_session_operator(request)
        if op is None or not op.user_id:
            return _redirect("/account",
                             "Password change is only available for DB-backed accounts.",
                             "error")
        if new_password != confirm_password:
            return _redirect("/account", "Passwords do not match.", "error")
        if len(new_password) < 8:
            return _redirect("/account", "Password must be at least 8 characters.", "error")
        try:
            await get_client().update_user(op.user_id, {"password": new_password})
            return _redirect("/account", "Password updated.")
        except MCPError as exc:
            return _redirect("/account", str(exc), "error")

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
