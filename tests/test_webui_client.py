"""
Tests for webui/client.py — the async MCPClient used by every Web UI route.

Up to now this module was exercised only transitively via AsyncMock
fakes in test_webui.py, which means shape bugs in the URL / header /
parsing layer would have slipped through. This suite stubs httpx at the
transport layer so every method's real wire behavior is asserted.
"""

from __future__ import annotations

import json

import httpx
import pytest

from webui.client import MCPClient, MCPError


# ---------------------------------------------------------------------------
# Recorder + transport
# ---------------------------------------------------------------------------

class Recorder:
    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []


def _mock_transport(
    recorder: Recorder,
    responses: dict[tuple[str, str], tuple[int, dict | str | bytes]],
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        recorder.calls.append(request)
        key = (request.method, request.url.path)
        if key not in responses:
            return httpx.Response(500, json={"detail": f"unmocked {key!r}"})
        status, body = responses[key]
        if isinstance(body, (bytes,)):
            return httpx.Response(status, content=body)
        if isinstance(body, str):
            return httpx.Response(status, content=body.encode())
        return httpx.Response(status, json=body)
    return httpx.MockTransport(handler)


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("MCP_SERVER_URL", "http://mock-catalog")
    monkeypatch.setenv("MCP_ADMIN_KEY", "ctest-admin-key")
    from webui.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def patched_asyncclient(monkeypatch, env):
    """Replace httpx.AsyncClient so every MCPClient HTTP call goes through
    our MockTransport. Tests pre-register responses per call via
    `cfg.responses[...] = ...`."""
    rec = Recorder()
    responses: dict[tuple[str, str], tuple[int, dict | str | bytes]] = {}
    real_async_client = httpx.AsyncClient

    def fake_async_client(*args, base_url=None, **kwargs):
        kwargs.pop("transport", None)
        return real_async_client(
            base_url=base_url or "http://mock-catalog",
            transport=_mock_transport(rec, responses),
            **kwargs,
        )

    monkeypatch.setattr(httpx, "AsyncClient", fake_async_client)

    # Return a small holder so tests can set responses AND read calls.
    class Cfg:
        pass
    Cfg.responses = responses
    Cfg.calls = rec.calls
    return Cfg


# ---------------------------------------------------------------------------
# Admin-key header gets attached to every request
# ---------------------------------------------------------------------------

class TestAdminKeyHeader:
    @pytest.mark.asyncio
    async def test_list_skills_sends_admin_key(self, patched_asyncclient):
        patched_asyncclient.responses[("GET", "/admin/skills")] = (200, [])
        await MCPClient().list_skills()
        assert patched_asyncclient.calls[0].headers["x-admin-key"] == "ctest-admin-key"


# ---------------------------------------------------------------------------
# Skillsets
# ---------------------------------------------------------------------------

class TestSkillsets:
    @pytest.mark.asyncio
    async def test_list_skillsets(self, patched_asyncclient):
        patched_asyncclient.responses[("GET", "/skillsets")] = (200, [{"id": "a"}])
        out = await MCPClient().list_skillsets()
        assert out == [{"id": "a"}]

    @pytest.mark.asyncio
    async def test_get_skillset(self, patched_asyncclient):
        patched_asyncclient.responses[("GET", "/skillsets/a")] = (200, {"id": "a"})
        out = await MCPClient().get_skillset("a")
        assert out["id"] == "a"

    @pytest.mark.asyncio
    async def test_create_skillset_sends_json(self, patched_asyncclient):
        patched_asyncclient.responses[("POST", "/skillsets")] = (201, {"id": "a"})
        await MCPClient().create_skillset({"id": "a", "name": "A"})
        body = json.loads(patched_asyncclient.calls[0].content)
        assert body == {"id": "a", "name": "A"}

    @pytest.mark.asyncio
    async def test_update_skillset_uses_put(self, patched_asyncclient):
        patched_asyncclient.responses[("PUT", "/skillsets/a")] = (200, {"id": "a"})
        await MCPClient().update_skillset("a", {"name": "A"})
        req = patched_asyncclient.calls[0]
        assert req.method == "PUT"

    @pytest.mark.asyncio
    async def test_delete_skillset_204_returns_none(self, patched_asyncclient):
        patched_asyncclient.responses[("DELETE", "/skillsets/a")] = (204, "")
        out = await MCPClient().delete_skillset("a")
        assert out is None

    @pytest.mark.asyncio
    async def test_list_skillset_skills_uses_admin_alias(self, patched_asyncclient):
        # Web UI always reads via /admin/... because it holds no JWT.
        patched_asyncclient.responses[("GET", "/admin/skillsets/a/skills")] = (200, [])
        await MCPClient().list_skillset_skills("a")
        assert patched_asyncclient.calls[0].url.path == "/admin/skillsets/a/skills"

    @pytest.mark.asyncio
    async def test_associate_skill_uses_put(self, patched_asyncclient):
        patched_asyncclient.responses[("PUT", "/skillsets/a/skills/s1")] = (204, "")
        await MCPClient().associate_skill("a", "s1")

    @pytest.mark.asyncio
    async def test_disassociate_skill_uses_delete(self, patched_asyncclient):
        patched_asyncclient.responses[("DELETE", "/skillsets/a/skills/s1")] = (204, "")
        await MCPClient().disassociate_skill("a", "s1")


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

class TestSkills:
    @pytest.mark.asyncio
    async def test_list_skills_uses_admin_endpoint(self, patched_asyncclient):
        patched_asyncclient.responses[("GET", "/admin/skills")] = (200, [])
        await MCPClient().list_skills()
        assert patched_asyncclient.calls[0].url.path == "/admin/skills"

    @pytest.mark.asyncio
    async def test_get_skill_latest(self, patched_asyncclient):
        patched_asyncclient.responses[("GET", "/admin/skills/s1")] = (200, {"id": "s1"})
        await MCPClient().get_skill("s1")
        req = patched_asyncclient.calls[0]
        assert req.url.path == "/admin/skills/s1"
        # No version query param when omitted.
        assert "version" not in dict(req.url.params)

    @pytest.mark.asyncio
    async def test_get_skill_specific_version_sends_param(self, patched_asyncclient):
        patched_asyncclient.responses[("GET", "/admin/skills/s1")] = (200, {})
        await MCPClient().get_skill("s1", version="1.2.3")
        assert dict(patched_asyncclient.calls[0].url.params) == {"version": "1.2.3"}

    @pytest.mark.asyncio
    async def test_list_skill_versions(self, patched_asyncclient):
        patched_asyncclient.responses[("GET", "/admin/skills/s1/versions")] = (200, [])
        await MCPClient().list_skill_versions("s1")

    @pytest.mark.asyncio
    async def test_create_skill_uses_post(self, patched_asyncclient):
        patched_asyncclient.responses[("POST", "/skills")] = (201, {})
        await MCPClient().create_skill({"id": "s1", "name": "S", "version": "1.0.0"})

    @pytest.mark.asyncio
    async def test_update_skill_uses_put(self, patched_asyncclient):
        patched_asyncclient.responses[("PUT", "/skills/s1")] = (200, {})
        await MCPClient().update_skill("s1", {"name": "S", "version": "1.0.0"})

    @pytest.mark.asyncio
    async def test_delete_skill_no_version(self, patched_asyncclient):
        patched_asyncclient.responses[("DELETE", "/skills/s1")] = (204, "")
        await MCPClient().delete_skill("s1")
        assert "version" not in dict(patched_asyncclient.calls[0].url.params)

    @pytest.mark.asyncio
    async def test_delete_skill_version_sends_param(self, patched_asyncclient):
        patched_asyncclient.responses[("DELETE", "/skills/s1")] = (204, "")
        await MCPClient().delete_skill("s1", version="1.2.3")
        assert dict(patched_asyncclient.calls[0].url.params) == {"version": "1.2.3"}


# ---------------------------------------------------------------------------
# Bundles
# ---------------------------------------------------------------------------

class TestBundles:
    @pytest.mark.asyncio
    async def test_list_bundle_files_uses_admin_path(self, patched_asyncclient):
        patched_asyncclient.responses[
            ("GET", "/admin/skills/s1/versions/1.0.0/files")
        ] = (200, [])
        await MCPClient().list_bundle_files("s1", "1.0.0")

    @pytest.mark.asyncio
    async def test_get_bundle_file_returns_raw_bytes(self, patched_asyncclient):
        """This method uses a separate httpx.AsyncClient (not _request) because
        the response is binary, not JSON."""
        patched_asyncclient.responses[
            ("GET", "/admin/skills/s1/versions/1.0.0/files/SKILL.md")
        ] = (200, b"# hi there")
        data = await MCPClient().get_bundle_file("s1", "1.0.0", "SKILL.md")
        assert data == b"# hi there"

    @pytest.mark.asyncio
    async def test_upload_bundle_sends_multipart(self, patched_asyncclient):
        patched_asyncclient.responses[
            ("POST", "/skills/s1/versions/1.0.0/bundle")
        ] = (201, {"file_count": 1, "total_size": 5})
        out = await MCPClient().upload_bundle(
            "s1", "1.0.0", "b.zip", b"PK\x03\x04data"
        )
        assert out == {"file_count": 1, "total_size": 5}
        req = patched_asyncclient.calls[0]
        ct = req.headers["content-type"]
        assert ct.startswith("multipart/form-data;")
        assert req.headers["x-admin-key"] == "ctest-admin-key"

    @pytest.mark.asyncio
    async def test_copy_bundle_hits_cross_skill_endpoint(self, patched_asyncclient):
        patched_asyncclient.responses[
            ("POST", "/skills/dst/versions/2.0.0/bundle/copy-from/src/1.0.0")
        ] = (201, {"file_count": 3, "total_size": 30})
        out = await MCPClient().copy_bundle("dst", "2.0.0", "src", "1.0.0")
        assert out["file_count"] == 3

    @pytest.mark.asyncio
    async def test_delete_bundle(self, patched_asyncclient):
        patched_asyncclient.responses[
            ("DELETE", "/skills/s1/versions/1.0.0/bundle")
        ] = (204, "")
        await MCPClient().delete_bundle("s1", "1.0.0")


# ---------------------------------------------------------------------------
# Error handling — MCPError shape
# ---------------------------------------------------------------------------

class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_http_error_surfaces_as_MCPError_with_detail(
        self, patched_asyncclient
    ):
        patched_asyncclient.responses[("GET", "/skillsets")] = (
            404,
            {"detail": "not there", "code": "HTTP_404"},
        )
        with pytest.raises(MCPError) as exc_info:
            await MCPClient().list_skillsets()
        assert "not there" in str(exc_info.value)
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_non_json_error_body_falls_back_to_text(
        self, patched_asyncclient
    ):
        patched_asyncclient.responses[("GET", "/skillsets")] = (500, "plain text error")
        with pytest.raises(MCPError) as exc_info:
            await MCPClient().list_skillsets()
        assert "plain text error" in str(exc_info.value)
        assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_connection_error_surfaces_as_MCPError(self, monkeypatch, env):
        """Simulate network failure (DNS / refused) by raising from the transport."""
        real_async_client = httpx.AsyncClient

        def handler(request):
            raise httpx.ConnectError("refused", request=request)

        def fake_async_client(*args, base_url=None, **kwargs):
            kwargs.pop("transport", None)
            return real_async_client(
                base_url=base_url or "http://mock-catalog",
                transport=httpx.MockTransport(handler),
                **kwargs,
            )

        monkeypatch.setattr(httpx, "AsyncClient", fake_async_client)

        with pytest.raises(MCPError) as exc_info:
            await MCPClient().list_skillsets()
        assert "Could not reach MCP server" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

class TestAgents:
    @pytest.mark.asyncio
    async def test_list_agents(self, patched_asyncclient):
        patched_asyncclient.responses[("GET", "/agents")] = (200, [])
        out = await MCPClient().list_agents()
        assert out == []
