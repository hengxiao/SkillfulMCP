"""MCPClient wrappers for Wave 9.1 account + signup surface.

End-to-end via an ASGI MockTransport against a live catalog app —
same pattern test_e2e.py uses, so the wrappers are verified with the
real router, not a stub.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from mcp_server.main import create_app
from webui.client import MCPClient


@pytest.fixture()
def catalog_asgi(monkeypatch):
    """Spin up a catalog app and wire MCPClient to talk to it through
    an httpx MockTransport. Returns a factory that gives each test
    its own MCPClient."""
    app = create_app(database_url="sqlite:///:memory:")
    # Need the lifespan so accounts / superadmin bootstrap run.
    tc = TestClient(app).__enter__()

    class _ASGITransportClient(httpx.AsyncClient):
        """AsyncClient that hits the ASGI app directly — no network."""

        def __init__(self, **kwargs):
            transport = httpx.ASGITransport(app=app)
            super().__init__(transport=transport, **kwargs)

    # Patch httpx.AsyncClient in the MCPClient module so the real
    # code path is exercised against the in-process app.
    import webui.client as webui_client_mod

    original = webui_client_mod.httpx.AsyncClient
    monkeypatch.setattr(webui_client_mod.httpx, "AsyncClient", _ASGITransportClient)

    yield MCPClient()

    monkeypatch.setattr(webui_client_mod.httpx, "AsyncClient", original)
    tc.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_create_account_via_client(catalog_asgi: MCPClient):
    client = catalog_asgi
    user = await client.signup(
        email="founder@corp.com",
        password="s3cret-pass",
        display_name="Founder",
    )
    account = await client.create_account(
        name="Corp", initial_admin_user_id=user["id"]
    )
    assert account["name"] == "Corp"

    accounts = await client.list_accounts()
    assert any(a["id"] == account["id"] for a in accounts)


@pytest.mark.asyncio
async def test_invite_and_list_via_client(catalog_asgi: MCPClient):
    client = catalog_asgi
    u = await client.signup(email="admin2@x.com", password="s3cret-pass")
    a = await client.create_account(
        name="T", initial_admin_user_id=u["id"]
    )

    # Invite an unregistered email → pending.
    invite = await client.invite_member(
        a["id"], email="future@x.com", role="viewer"
    )
    assert invite["pending"] is True

    members = await client.list_members(a["id"])
    pending_rows = [m for m in members if m.get("pending")]
    assert any(m["email"] == "future@x.com" for m in pending_rows)


@pytest.mark.asyncio
async def test_disable_via_client(catalog_asgi: MCPClient):
    client = catalog_asgi
    u = await client.signup(email="boring@x.com", password="s3cret-pass")
    out = await client.set_user_disabled(u["id"], True)
    assert out["disabled"] is True
    # authenticate_user returns None on 401.
    result = await client.authenticate_user("boring@x.com", "s3cret-pass")
    assert result is None


@pytest.mark.asyncio
async def test_delete_pending_via_client(catalog_asgi: MCPClient):
    client = catalog_asgi
    u = await client.signup(email="owner3@x.com", password="s3cret-pass")
    a = await client.create_account(name="Del", initial_admin_user_id=u["id"])
    p = await client.invite_member(
        a["id"], email="gone@x.com", role="viewer"
    )
    await client.delete_pending_invite(a["id"], p["id"])
    members = await client.list_members(a["id"])
    assert all(m.get("email") != "gone@x.com" for m in members)


@pytest.mark.asyncio
async def test_delete_account_via_client(catalog_asgi: MCPClient):
    client = catalog_asgi
    u = await client.signup(email="shortlived@x.com", password="s3cret-pass")
    a = await client.create_account(
        name="ShortLived", initial_admin_user_id=u["id"]
    )
    # One member (the admin).
    await client.delete_account(
        a["id"], confirm_user_count=1
    )
    accounts = await client.list_accounts()
    assert not any(row["id"] == a["id"] for row in accounts)
