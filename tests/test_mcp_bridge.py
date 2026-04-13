"""Unit tests for tools/mcp-bridge/skillful_bridge.py.

Tests the three tool handlers against a stubbed catalog via
`httpx.MockTransport`. The MCP server itself is wired but not
exercised — that requires a real stdio harness; the handlers are
the part that can break in interesting ways.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import httpx
import pytest


# Load the bridge module by path since it isn't a package member.
_BRIDGE_PATH = (
    Path(__file__).resolve().parent.parent
    / "tools" / "mcp-bridge" / "skillful_bridge.py"
)


def _load_bridge(monkeypatch, *, token="t-jwt", admin_key="", catalog="https://c.example.com"):
    """Re-import the bridge with a fresh env so module-level
    constants (CATALOG, TOKEN, ADMIN_KEY) reflect the test config."""
    monkeypatch.setenv("MCP_CATALOG_URL", catalog)
    monkeypatch.setenv("MCP_CATALOG_TOKEN", token)
    if admin_key:
        monkeypatch.setenv("MCP_CATALOG_ADMIN_KEY", admin_key)
    else:
        monkeypatch.delenv("MCP_CATALOG_ADMIN_KEY", raising=False)
    sys.modules.pop("skillful_bridge", None)
    spec = importlib.util.spec_from_file_location("skillful_bridge", _BRIDGE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_transport(routes):
    """`routes` is a dict of (method, path) -> (status, json_body OR text body)."""
    def handler(request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        if key not in routes:
            return httpx.Response(404, json={"error": f"no route for {key}"})
        status, body = routes[key]
        if isinstance(body, (dict, list)):
            return httpx.Response(status, json=body)
        return httpx.Response(status, text=body)
    return httpx.MockTransport(handler)


@pytest.fixture()
def bridge(monkeypatch):
    return _load_bridge(monkeypatch)


# ---------------------------------------------------------------------------
# list_skills
# ---------------------------------------------------------------------------

class TestListSkills:
    @pytest.mark.asyncio
    async def test_returns_summary_lines(self, bridge):
        transport = _make_transport({
            ("GET", "/skills"): (200, [
                {"id": "lookup-invoice", "version": "1.0.0",
                 "name": "Lookup Invoice",
                 "description": "Retrieve invoice details from CRM"},
                {"id": "deploy", "version": "2.1.0",
                 "name": "Deploy", "description": ""},
            ]),
        })
        async with httpx.AsyncClient(transport=transport) as c:
            text = await bridge._do_list_skills(c, query=None)
        assert "lookup-invoice (1.0.0)" in text
        assert "deploy (2.1.0)" in text
        # Description renders on the next line for non-empty rows.
        assert "Retrieve invoice details" in text

    @pytest.mark.asyncio
    async def test_filters_by_query(self, bridge):
        transport = _make_transport({
            ("GET", "/skills"): (200, [
                {"id": "lookup-invoice", "version": "1", "name": "X"},
                {"id": "deploy", "version": "1", "name": "Deploy"},
            ]),
        })
        async with httpx.AsyncClient(transport=transport) as c:
            text = await bridge._do_list_skills(c, query="invoice")
        assert "lookup-invoice" in text
        assert "deploy" not in text

    @pytest.mark.asyncio
    async def test_empty_match(self, bridge):
        transport = _make_transport({
            ("GET", "/skills"): (200, [
                {"id": "x", "version": "1", "name": "X"},
            ]),
        })
        async with httpx.AsyncClient(transport=transport) as c:
            text = await bridge._do_list_skills(c, query="nope")
        assert text == "No skills matched."

    @pytest.mark.asyncio
    async def test_admin_key_path_uses_admin_route(self, monkeypatch):
        """When admin-key auth is configured, the bridge hits
        /admin/skills (which surfaces extra fields like `verified`)
        instead of the JWT-scoped /skills."""
        bridge = _load_bridge(monkeypatch, token="", admin_key="key")
        transport = _make_transport({
            ("GET", "/admin/skills"): (200, [
                {"id": "s", "version": "1", "name": "S", "verified": True},
                {"id": "u", "version": "1", "name": "U", "verified": False},
            ]),
        })
        async with httpx.AsyncClient(transport=transport) as c:
            text = await bridge._do_list_skills(c, query=None)
        assert "✓" in text
        assert "⚠" in text


# ---------------------------------------------------------------------------
# get_skill
# ---------------------------------------------------------------------------

class TestGetSkill:
    @pytest.mark.asyncio
    async def test_returns_skill_md_with_header(self, bridge):
        transport = _make_transport({
            ("GET", "/skills/lookup-invoice"): (200, {
                "id": "lookup-invoice", "version": "1.2.0",
                "name": "Lookup Invoice", "visibility": "account",
            }),
            ("GET", "/skills/lookup-invoice/versions/1.2.0/files/SKILL.md"): (
                200, "# Lookup Invoice\n\nRun the lookup query."
            ),
        })
        async with httpx.AsyncClient(transport=transport) as c:
            out = await bridge._do_get_skill(c, "lookup-invoice", version=None)
        # Bridge prepends a small header before the SKILL.md body.
        assert "Lookup Invoice" in out
        assert "id: `lookup-invoice`" in out
        assert "v1.2.0" in out
        assert "Run the lookup query" in out
        assert "visibility: account" in out

    @pytest.mark.asyncio
    async def test_unknown_skill_raises(self, bridge):
        transport = _make_transport({
            ("GET", "/skills/ghost"): (200, {}),  # empty -> no version
        })
        async with httpx.AsyncClient(transport=transport) as c:
            with pytest.raises(ValueError, match="not found"):
                await bridge._do_get_skill(c, "ghost", version=None)


# ---------------------------------------------------------------------------
# download_skill
# ---------------------------------------------------------------------------

class TestDownloadSkill:
    @pytest.mark.asyncio
    async def test_writes_bundle_to_local_path(self, bridge, tmp_path, monkeypatch):
        # Redirect LOCAL_SKILLS to a temp dir.
        monkeypatch.setattr(bridge, "LOCAL_SKILLS", tmp_path)
        transport = _make_transport({
            ("GET", "/skills/demo"): (200, {"version": "1.0.0"}),
            ("GET", "/skills/demo/versions/1.0.0/files"): (200, [
                {"path": "SKILL.md"},
                {"path": "helpers/util.py"},
            ]),
            ("GET", "/skills/demo/versions/1.0.0/files/SKILL.md"): (
                200, "# Demo\n",
            ),
            ("GET", "/skills/demo/versions/1.0.0/files/helpers/util.py"): (
                200, "x = 1\n",
            ),
        })
        async with httpx.AsyncClient(transport=transport) as c:
            msg = await bridge._do_download_skill(
                c, "demo", version=None, require_signature=False,
            )
        assert "installed demo v1.0.0" in msg
        assert (tmp_path / "demo" / "SKILL.md").read_text() == "# Demo\n"
        assert (tmp_path / "demo" / "helpers" / "util.py").read_text() == "x = 1\n"

    @pytest.mark.asyncio
    async def test_wipes_stale_files_before_install(
        self, bridge, tmp_path, monkeypatch
    ):
        """A previous install left a now-removed file; it must not
        survive the next download."""
        monkeypatch.setattr(bridge, "LOCAL_SKILLS", tmp_path)
        old = tmp_path / "demo" / "old.txt"
        old.parent.mkdir(parents=True)
        old.write_text("stale")

        transport = _make_transport({
            ("GET", "/skills/demo"): (200, {"version": "2.0.0"}),
            ("GET", "/skills/demo/versions/2.0.0/files"): (
                200, [{"path": "SKILL.md"}]
            ),
            ("GET", "/skills/demo/versions/2.0.0/files/SKILL.md"): (
                200, "v2",
            ),
        })
        async with httpx.AsyncClient(transport=transport) as c:
            await bridge._do_download_skill(
                c, "demo", version=None, require_signature=False,
            )
        assert not old.exists()
        assert (tmp_path / "demo" / "SKILL.md").read_text() == "v2"

    @pytest.mark.asyncio
    async def test_require_signature_refuses_unverified(
        self, bridge, tmp_path, monkeypatch
    ):
        # Bridge needs admin-key auth to see the `verified` flag.
        bridge = _load_bridge(monkeypatch, token="", admin_key="k")
        monkeypatch.setattr(bridge, "LOCAL_SKILLS", tmp_path)
        transport = _make_transport({
            ("GET", "/admin/skills/demo"): (200, {
                "version": "1.0.0", "verified": False,
            }),
        })
        async with httpx.AsyncClient(transport=transport) as c:
            with pytest.raises(ValueError, match="signature did not verify"):
                await bridge._do_download_skill(
                    c, "demo", version=None, require_signature=True,
                )

    @pytest.mark.asyncio
    async def test_require_signature_without_admin_key_raises(
        self, bridge, tmp_path, monkeypatch
    ):
        """JWT-only auth doesn't expose `verified`; require_signature
        must surface the misconfig instead of silently installing."""
        monkeypatch.setattr(bridge, "LOCAL_SKILLS", tmp_path)
        transport = _make_transport({
            ("GET", "/skills/demo"): (200, {"version": "1.0.0"}),
            # No `verified` key — JWT path.
        })
        async with httpx.AsyncClient(transport=transport) as c:
            with pytest.raises(ValueError, match="MCP_CATALOG_ADMIN_KEY"):
                await bridge._do_download_skill(
                    c, "demo", version=None, require_signature=True,
                )


# ---------------------------------------------------------------------------
# Auth headers
# ---------------------------------------------------------------------------

class TestAuthHeaders:
    def test_jwt_wins(self, bridge):
        assert bridge._auth_headers() == {"Authorization": "Bearer t-jwt"}

    def test_admin_key_fallback(self, monkeypatch):
        bridge = _load_bridge(monkeypatch, token="", admin_key="k")
        assert bridge._auth_headers() == {"X-Admin-Key": "k"}
