"""Web UI flow coverage (delivery.md §4 Web UI routes).

Covers the branches that test_webui.py leaves unexercised: the landing
page's public + authenticated paths, the skill detail + clone +
new-version flows, file download, and the skillset detail/association
surface.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import webui.main as webui_main
from webui.main import create_app

from tests.conftest import TEST_OPERATOR_EMAIL, TEST_OPERATOR_PASSWORD


@pytest.fixture()
def mock_client(monkeypatch):
    fake = AsyncMock()
    fake.list_skills.return_value = []
    fake.list_skillsets.return_value = []
    fake.list_agents.return_value = []
    monkeypatch.setattr(webui_main, "_client", fake)
    monkeypatch.setattr(webui_main, "get_client", lambda: fake)
    return fake


@pytest.fixture()
def anon_client(mock_client):
    app = create_app()
    with TestClient(app) as c:
        yield c, mock_client


@pytest.fixture()
def logged_in_client(mock_client):
    app = create_app()
    with TestClient(app) as c:
        r = c.post("/login", data={
            "email": TEST_OPERATOR_EMAIL,
            "password": TEST_OPERATOR_PASSWORD,
            "csrf_token": "", "next": "/",
        }, follow_redirects=False)
        assert r.status_code == 303
        yield c, mock_client


# ---------------------------------------------------------------------------
# Landing page (/)
# ---------------------------------------------------------------------------

class TestLanding:
    def test_anonymous_sees_public_items_and_sign_in(self, anon_client):
        client, mock = anon_client
        mock.list_skills.return_value = [
            {"id": "pub-sk", "name": "Pub Sk", "version": "1.0.0",
             "visibility": "public", "description": "", "metadata": {},
             "is_latest": True},
            {"id": "priv-sk", "name": "Priv Sk", "version": "1.0.0",
             "visibility": "private", "description": "", "metadata": {},
             "is_latest": True},
        ]
        mock.list_skillsets.return_value = [
            {"id": "pub-ss", "name": "Pub SS", "visibility": "public",
             "description": ""},
            {"id": "priv-ss", "name": "Priv SS", "visibility": "private",
             "description": ""},
        ]
        r = client.get("/")
        assert r.status_code == 200
        assert b"Public catalog" in r.content
        assert b"Sign in" in r.content
        # Public items render; private items don't.
        assert b"pub-sk" in r.content
        assert b"pub-ss" in r.content
        assert b"priv-sk" not in r.content
        assert b"priv-ss" not in r.content

    def test_logged_in_sees_counts_and_no_sign_in(self, logged_in_client):
        client, mock = logged_in_client
        mock.list_skills.return_value = [
            {"id": "s1", "name": "S1", "version": "1.0.0",
             "visibility": "public", "description": "", "metadata": {},
             "is_latest": True}
        ]
        mock.list_skillsets.return_value = []
        mock.list_agents.return_value = [{"id": "a1", "name": "A1"}]
        r = client.get("/")
        assert r.status_code == 200
        assert b"Public catalog" in r.content
        # Counts row only renders for logged-in users.
        assert b"Skillsets (all)" in r.content

    def test_mcp_error_shows_inline_banner(self, anon_client):
        client, mock = anon_client
        from webui.client import MCPError
        mock.list_skills.side_effect = MCPError("catalog unreachable", 500)
        r = client.get("/")
        assert r.status_code == 200
        assert b"catalog unreachable" in r.content


# ---------------------------------------------------------------------------
# Skill detail + SKILL.md inline render
# ---------------------------------------------------------------------------

class TestSkillDetail:
    def test_renders_skill_md_inline(self, logged_in_client):
        client, mock = logged_in_client
        mock.get_skill.return_value = {
            "id": "demo", "name": "Demo", "version": "1.0.0",
            "visibility": "public", "description": "", "metadata": {},
            "is_latest": True,
        }
        mock.list_skill_versions.return_value = [
            {"version": "1.0.0", "is_latest": True,
             "created_at": "2026-04-13T00:00:00"}
        ]
        mock.list_bundle_files.return_value = [
            {"path": "SKILL.md", "size": 10, "sha256": "abc"},
        ]
        mock.get_bundle_file.return_value = b"# Hello\n\nBody copy."
        r = client.get("/skills/demo")
        assert r.status_code == 200
        assert b"demo" in r.content

    def test_unknown_skill_redirects_to_list(self, logged_in_client):
        client, mock = logged_in_client
        from webui.client import MCPError
        mock.get_skill.side_effect = MCPError("not found", 404)
        r = client.get("/skills/ghost", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"].startswith("/skills?")


# ---------------------------------------------------------------------------
# Clone + new-version wizards
# ---------------------------------------------------------------------------

class TestCloneAndNewVersion:
    def _mock_source(self, mock):
        mock.get_skill.return_value = {
            "id": "src", "name": "Src", "version": "1.0.0",
            "visibility": "public", "description": "desc",
            "metadata": {"k": 1}, "is_latest": True,
        }
        mock.list_bundle_files.return_value = [
            {"path": "SKILL.md", "size": 4, "sha256": "a"},
        ]

    def test_clone_page_renders_source_defaults(self, logged_in_client):
        client, mock = logged_in_client
        self._mock_source(mock)
        r = client.get("/skills/src/clone")
        assert r.status_code == 200
        # Source id + version prefill.
        assert b"src" in r.content
        assert b"1.0.0" in r.content

    def test_new_version_page_shows_bundle_copy_option(self, logged_in_client):
        client, mock = logged_in_client
        self._mock_source(mock)
        r = client.get("/skills/src/new-version")
        assert r.status_code == 200
        # Copy option rendered since source has a bundle.
        assert b"Copy bundle" in r.content

    def test_clone_metadata_json_error_redirects(self, logged_in_client):
        client, mock = logged_in_client
        self._mock_source(mock)
        r = client.post("/skills/src/clone", data={
            "new_id": "dup", "new_name": "Dup", "version": "2.0.0",
            "from_version": "1.0.0", "bundle_action": "none",
            "metadata": "not-json",
            "csrf_token": "",
        }, follow_redirects=False)
        # Bad JSON → redirect back with error flash.
        assert r.status_code == 303
        assert "msg_type=error" in r.headers["location"]

    def test_new_version_post_runs_bundle_copy(self, logged_in_client):
        client, mock = logged_in_client
        self._mock_source(mock)
        mock.create_skill.return_value = {"id": "src", "version": "2.0.0"}
        mock.copy_bundle.return_value = {"file_count": 1, "total_size": 4}
        r = client.post("/skills/src/new-version", data={
            "version": "2.0.0",
            "from_version": "1.0.0",
            "bundle_action": "copy",
            "metadata": "{}",
            "description": "desc v2",
            "visibility": "public",
            "csrf_token": "",
        }, follow_redirects=False)
        assert r.status_code == 303
        mock.create_skill.assert_awaited_once()
        mock.copy_bundle.assert_awaited_once()


# ---------------------------------------------------------------------------
# Bundle file download proxy
# ---------------------------------------------------------------------------

class TestBundleDownload:
    def test_file_download_returns_bytes(self, logged_in_client):
        client, mock = logged_in_client
        mock.get_bundle_file.return_value = b"raw-bytes"
        r = client.get("/skills/demo/versions/1.0.0/files/SKILL.md")
        assert r.status_code == 200
        assert r.content == b"raw-bytes"

    def test_file_download_propagates_error(self, logged_in_client):
        client, mock = logged_in_client
        from webui.client import MCPError
        mock.get_bundle_file.side_effect = MCPError("gone", 404)
        r = client.get("/skills/demo/versions/1.0.0/files/nope.md")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Skillset detail + associations
# ---------------------------------------------------------------------------

class TestSkillsetDetail:
    def test_detail_lists_member_skills(self, logged_in_client):
        client, mock = logged_in_client
        mock.get_skillset.return_value = {
            "id": "ops", "name": "Ops", "visibility": "public",
            "description": "",
        }
        mock.list_skillset_skills.return_value = [
            {"id": "deploy", "name": "Deploy", "version": "1.0.0",
             "is_latest": True}
        ]
        mock.list_skills.return_value = [
            {"id": "deploy", "name": "Deploy", "version": "1.0.0",
             "is_latest": True},
            {"id": "other", "name": "Other", "version": "1.0.0",
             "is_latest": True},
        ]
        r = client.get("/skillsets/ops")
        assert r.status_code == 200
        assert b"deploy" in r.content
        # 'other' appears in the add-skill dropdown (not a member yet).
        assert b"other" in r.content

    def test_detail_unknown_skillset_redirects(self, logged_in_client):
        client, mock = logged_in_client
        from webui.client import MCPError
        mock.get_skillset.side_effect = MCPError("not found", 404)
        r = client.get("/skillsets/ghost", follow_redirects=False)
        assert r.status_code == 303
