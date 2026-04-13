"""
End-to-end integration tests.

Wires a real catalog app and a real webui app in-process, routing the
webui's `MCPClient` through an `httpx.ASGITransport` that dispatches into
the catalog's ASGI handler directly — no sockets, no uvicorn. Every
request goes through every layer: SessionMiddleware → AuthMiddleware →
csrf_required dep → webui handler → MCPClient → ASGITransport → catalog
middleware stack → router → service → DB.

This catches contract drift that the per-module unit tests miss:
- The Web UI asks for `/admin/skills` but the catalog moved it to
  `/v1/admin/skills` — caught here.
- The catalog changes a JSON field name — caught here.
- The webui forwards headers the catalog no longer expects — caught here.

Runs against in-memory SQLite catalog; no S3, no Postgres.
"""

from __future__ import annotations

import io
import re
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

import webui.main as webui_main
from mcp_server.main import create_app as create_catalog_app
from webui.client import MCPClient
from webui.main import create_app as create_webui_app

from tests.conftest import TEST_OPERATOR_EMAIL, TEST_OPERATOR_PASSWORD, ADMIN_KEY


# ---------------------------------------------------------------------------
# The wiring: ASGITransport + real MCPClient + webui TestClient
# ---------------------------------------------------------------------------

@pytest.fixture()
def stack(monkeypatch):
    """Build catalog + webui, wire the webui's MCPClient to dispatch into
    the catalog app via ASGITransport, log the test operator in, return
    the authenticated webui client."""
    # Real catalog app, fresh in-memory DB. `with TestClient(...)` is what
    # actually runs the lifespan (session_factory init). Keep the context
    # manager alive for the whole fixture.
    catalog_app = create_catalog_app(database_url="sqlite:///:memory:")
    catalog_test_client = TestClient(catalog_app).__enter__()

    # Route the webui's async httpx calls into the catalog's ASGI stack.
    # We rebuild every MCPClient method that opens an AsyncClient to use
    # ASGITransport instead of the network.
    def _open_asgi_client(timeout):
        return httpx.AsyncClient(
            base_url="http://catalog",
            transport=httpx.ASGITransport(app=catalog_app),
            timeout=timeout,
        )

    async def _request(self, method, path, **kwargs):
        async with _open_asgi_client(10) as c:
            try:
                r = await c.request(method, path, headers=self._headers, **kwargs)
                r.raise_for_status()
            except httpx.HTTPStatusError as exc:
                from webui.client import MCPError, _extract_detail
                raise MCPError(_extract_detail(exc), exc.response.status_code) from exc
            except httpx.RequestError as exc:
                from webui.client import MCPError
                raise MCPError(
                    f"Could not reach MCP server at {self._base_url}: {exc}"
                ) from exc
        if r.status_code == 204:
            return None
        return r.json()

    async def _get_bundle_file(self, skill_id, version, path):
        async with _open_asgi_client(30) as c:
            r = await c.get(
                f"/admin/skills/{skill_id}/versions/{version}/files/{path}",
                headers=self._headers,
            )
            r.raise_for_status()
            return r.content

    async def _upload_bundle(self, skill_id, version, filename, data):
        async with _open_asgi_client(60) as c:
            r = await c.post(
                f"/skills/{skill_id}/versions/{version}/bundle",
                headers=self._headers,
                files={"file": (filename, data, "application/octet-stream")},
            )
            try:
                r.raise_for_status()
            except httpx.HTTPStatusError as exc:
                from webui.client import MCPError, _extract_detail
                raise MCPError(_extract_detail(exc), exc.response.status_code) from exc
            return r.json()

    async def _copy_bundle(self, dst_skill_id, dst_version, src_skill_id, src_version):
        return await self._request(
            "POST",
            f"/skills/{dst_skill_id}/versions/{dst_version}"
            f"/bundle/copy-from/{src_skill_id}/{src_version}",
        )

    monkeypatch.setattr(MCPClient, "_request", _request)
    monkeypatch.setattr(MCPClient, "get_bundle_file", _get_bundle_file)
    monkeypatch.setattr(MCPClient, "upload_bundle", _upload_bundle)
    monkeypatch.setattr(MCPClient, "copy_bundle", _copy_bundle)
    # Force the webui to build a fresh MCPClient that picks up the patched methods.
    monkeypatch.setattr(webui_main, "_client", None)

    # Prepare the catalog: create the operator-facing agent + a skillset + skill
    # via the REAL admin API so later webui flows have something to list.
    admin = {"X-Admin-Key": ADMIN_KEY}
    catalog_test_client.post("/skillsets",
        json={"id": "billing", "name": "Billing"}, headers=admin).raise_for_status()
    catalog_test_client.post("/skills",
        json={"id": "lookup-invoice", "name": "Lookup",
              "version": "1.0.0", "metadata": {"x": 1},
              "skillset_ids": ["billing"]},
        headers=admin).raise_for_status()
    catalog_test_client.post("/agents",
        json={"id": "billing-agent", "name": "Billing Agent",
              "skillsets": ["billing"], "scope": ["read"]},
        headers=admin).raise_for_status()

    # Webui TestClient + auto-login.
    webui_app = create_webui_app()
    webui_client = TestClient(webui_app)
    r = webui_client.post("/login", data={
        "email": TEST_OPERATOR_EMAIL,
        "password": TEST_OPERATOR_PASSWORD,
        "csrf_token": "",
        "next": "/",
    }, follow_redirects=False)
    assert r.status_code == 303, r.text[:300]

    yield webui_client, catalog_test_client


# ---------------------------------------------------------------------------
# End-to-end scenarios
# ---------------------------------------------------------------------------

class TestDashboard:
    def test_dashboard_shows_seeded_counts(self, stack):
        webui_client, _catalog = stack
        r = webui_client.get("/")
        assert r.status_code == 200
        # Counts come from the real catalog: 1 skillset, 1 skill, 1 agent.
        assert b">1<" in r.content or b">\n      1\n" in r.content


class TestSkillListingAndFilter:
    def test_skills_page_renders_real_catalog_data(self, stack):
        webui_client, _catalog = stack
        r = webui_client.get("/skills")
        assert r.status_code == 200
        # The seeded skill shows up with its real id.
        assert b"lookup-invoice" in r.content
        # And carries the real skillset membership as a data attr —
        # proving the webui resolved it via the catalog's admin endpoint.
        assert b'data-skillsets="billing"' in r.content


class TestSkillDetailQuickViewModal:
    def test_modal_partial_lists_versions_and_metadata(self, stack):
        webui_client, _catalog = stack
        r = webui_client.get("/skills/lookup-invoice/modal")
        assert r.status_code == 200
        assert b"v1.0.0" in r.content


class TestNewVersionFlowCreatesRealRow(object):
    def test_post_new_version_writes_to_catalog(self, stack):
        webui_client, catalog = stack
        # Post through the webui, then verify via the catalog's admin API
        # that the row really exists.
        r = webui_client.post(
            "/skills/lookup-invoice/new-version",
            data={
                "version": "1.1.0",
                "from_version": "1.0.0",
                "bundle_action": "none",
                "description": "bump",
                "metadata": "{}",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        # Verify end-to-end: the catalog's version list now includes 1.1.0.
        admin = {"X-Admin-Key": ADMIN_KEY}
        vs = catalog.get(
            "/admin/skills/lookup-invoice/versions", headers=admin
        ).json()
        assert any(v["version"] == "1.1.0" for v in vs)


class TestBundleRoundTripAcrossTheStack:
    def test_upload_via_webui_view_via_webui(self, stack):
        webui_client, catalog = stack
        # 1. Create a new version we can attach a bundle to.
        admin = {"X-Admin-Key": ADMIN_KEY}
        r = webui_client.post(
            "/skills/lookup-invoice/new-version",
            data={
                "version": "2.0.0",
                "from_version": "1.0.0",
                "bundle_action": "upload",
                "description": "bundle via webui",
                "metadata": "{}",
            },
            files={
                "file": (
                    "b.zip",
                    self._zip_bytes({"SKILL.md": b"# hello", "x/y.txt": b"z"}),
                    "application/zip",
                ),
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        # 2. Reading the file back through the webui's proxy.
        fr = webui_client.get(
            "/skills/lookup-invoice/versions/2.0.0/files/SKILL.md"
        )
        assert fr.status_code == 200
        assert fr.content == b"# hello"
        # 3. Cross-check with the catalog's admin listing directly.
        files = catalog.get(
            "/admin/skills/lookup-invoice/versions/2.0.0/files", headers=admin
        ).json()
        assert sorted(f["path"] for f in files) == ["SKILL.md", "x/y.txt"]

    @staticmethod
    def _zip_bytes(entries: dict[str, bytes]) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for p, c in entries.items():
                zf.writestr(p, c)
        return buf.getvalue()


class TestCloneSkillCreatesNewSkillViaCatalog:
    def test_clone_end_to_end(self, stack):
        webui_client, catalog = stack
        admin = {"X-Admin-Key": ADMIN_KEY}
        # Seed a bundle on the source so "copy" has something to copy.
        _ = webui_client.post(
            "/skills/lookup-invoice/new-version",
            data={
                "version": "3.0.0",
                "from_version": "1.0.0",
                "bundle_action": "upload",
                "description": "src-for-clone",
                "metadata": "{}",
            },
            files={
                "file": (
                    "b.zip",
                    TestBundleRoundTripAcrossTheStack._zip_bytes({"a.txt": b"1"}),
                    "application/zip",
                ),
            },
            follow_redirects=False,
        )
        r = webui_client.post(
            "/skills/lookup-invoice/clone",
            data={
                "new_id": "lookup-invoice-copy",
                "new_name": "Lookup Invoice (clone)",
                "version": "1.0.0",
                "from_version": "3.0.0",
                "bundle_action": "copy",
                "description": "cloned",
                "metadata": "{}",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        # New skill in the catalog.
        skill = catalog.get(
            "/admin/skills/lookup-invoice-copy", headers=admin
        ).json()
        assert skill["name"] == "Lookup Invoice (clone)"
        # Bundle copied across.
        files = catalog.get(
            "/admin/skills/lookup-invoice-copy/versions/1.0.0/files", headers=admin
        ).json()
        assert [f["path"] for f in files] == ["a.txt"]
