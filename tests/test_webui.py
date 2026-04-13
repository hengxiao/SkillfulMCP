"""
Integration tests for the Web UI.

The real MCPClient is replaced with an AsyncMock so these tests exercise the
webui routes and templates without a running MCP server. The focus is on the
flows that previously shipped untested:

- _redirect helper (regression: produced illegal "?x=1?msg=..." URLs when the
  path already had a query string, which broke the new-version and clone
  flows — handlers saw a garbled version and reported "Skill not found").
- New-version POST: bundle actions (copy / upload / none), immutable name,
  clean redirect URL, metadata validation.
- Clone POST: new skill id, cross-skill bundle copy, clean redirect URL.
- View page: unknown-version redirect stays within one '?'.
- Skills list: search / skillset-filter markup rendered from server data.
"""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

# conftest.py sets MCP_ADMIN_KEY and other env vars before importing
# mcp_server; those settings also satisfy the webui's Settings class.

import webui.main as webui_main
from webui.client import MCPError
from webui.main import _redirect, create_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _base_skill(**overrides):
    base = {
        "id": "pdf",
        "name": "PDF Toolkit",
        "version": "1.0.0",
        "description": "desc",
        "is_latest": True,
        "metadata": {"hint": "ocr"},
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
    }
    base.update(overrides)
    return base


@pytest.fixture()
def fake_mcp(monkeypatch):
    """Replace webui's MCPClient with an AsyncMock for all tests."""
    fake = AsyncMock()
    # Reasonable defaults; tests override as needed.
    fake.list_skillsets.return_value = []
    fake.list_skills.return_value = []
    fake.list_agents.return_value = []
    fake.list_bundle_files.return_value = []
    fake.list_skill_versions.return_value = []
    fake.list_skillset_skills.return_value = []
    monkeypatch.setattr(webui_main, "_client", fake)
    monkeypatch.setattr(webui_main, "get_client", lambda: fake)
    return fake


@pytest.fixture()
def webui(fake_mcp):
    """Authenticated Web UI TestClient.

    Builds the app, logs in the test operator, and yields the client with
    the session cookie attached. CSRF is disabled in the default test env
    (see conftest.py) so these tests don't have to thread tokens through
    every POST — the dedicated CSRF suite in test_webui_auth.py covers
    that separately.
    """
    from tests.conftest import TEST_OPERATOR_EMAIL, TEST_OPERATOR_PASSWORD
    app = create_app()
    with TestClient(app) as c:
        r = c.post(
            "/login",
            data={
                "email": TEST_OPERATOR_EMAIL,
                "password": TEST_OPERATOR_PASSWORD,
                "csrf_token": "",  # CSRF off in test env
                "next": "/",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303, (
            f"login fixture failed: {r.status_code} {r.text[:200]}"
        )
        yield c, fake_mcp


# ---------------------------------------------------------------------------
# _redirect — unit
# ---------------------------------------------------------------------------

class TestRedirect:
    def test_no_msg(self):
        assert _redirect("/skills").headers["location"] == "/skills"

    def test_plain_path_gets_question_mark(self):
        loc = _redirect("/skills", "ok").headers["location"]
        assert loc.startswith("/skills?msg=ok")
        assert loc.count("?") == 1

    def test_path_with_query_uses_ampersand(self):
        """Regression for the 'Skill not found' bug on new-version save."""
        loc = _redirect("/skills/pdf?version=1.0.0", "ok").headers["location"]
        assert loc.startswith("/skills/pdf?version=1.0.0&msg=ok")
        assert loc.count("?") == 1

    def test_msg_is_url_quoted(self):
        loc = _redirect("/skills", "a b & c").headers["location"]
        assert "a%20b%20%26%20c" in loc or "a+b+%26+c" in loc

    def test_msg_type_defaults_to_success(self):
        loc = _redirect("/skills", "hi").headers["location"]
        assert "msg_type=success" in loc

    def test_error_msg_type(self):
        loc = _redirect("/skills", "oops", "error").headers["location"]
        assert "msg_type=error" in loc


# ---------------------------------------------------------------------------
# New-version flow
# ---------------------------------------------------------------------------

class TestNewVersionPage:
    def test_get_page_prefilled_and_name_readonly(self, webui):
        client, fake = webui
        fake.get_skill.return_value = _base_skill()
        fake.list_bundle_files.return_value = [
            {"path": "SKILL.md", "size": 10, "sha256": "x"}
        ]
        r = client.get("/skills/pdf/new-version?from=1.0.0")
        assert r.status_code == 200
        body = r.content
        assert b"v1.0.0" in body
        # Name input is present but disabled/readonly (immutable).
        assert b"readonly" in body
        # Copy-from option is pre-selected because source has a bundle.
        assert b'value="copy"' in body and b"checked" in body

    def test_get_without_from_uses_latest(self, webui):
        client, fake = webui
        fake.get_skill.return_value = _base_skill(version="2.0.0")
        r = client.get("/skills/pdf/new-version")
        assert r.status_code == 200
        # get_skill called with version=None (i.e. latest)
        args, kwargs = fake.get_skill.call_args
        assert kwargs.get("version") is None

    def test_get_unknown_source_redirects(self, webui):
        client, fake = webui
        fake.get_skill.side_effect = MCPError("Skill not found", status_code=404)
        r = client.get(
            "/skills/nope/new-version?from=1.0.0", follow_redirects=False
        )
        assert r.status_code == 303
        assert r.headers["location"].startswith("/skills?msg=")


class TestNewVersionPost:
    def _base_form(self, **overrides):
        base = {
            "version": "2.0.0",
            "from_version": "1.0.0",
            "bundle_action": "copy",
            "description": "new desc",
            "metadata": "{}",
        }
        base.update(overrides)
        return base

    def test_copy_bundle_flow(self, webui):
        client, fake = webui
        fake.get_skill.return_value = _base_skill()
        fake.create_skill.return_value = {"id": "pdf", "version": "2.0.0"}
        fake.copy_bundle.return_value = {"file_count": 1, "total_size": 100}
        r = client.post(
            "/skills/pdf/new-version",
            data=self._base_form(),
            follow_redirects=False,
        )
        assert r.status_code == 303
        loc = r.headers["location"]
        # Regression: exactly one '?' in the redirect URL.
        assert loc.count("?") == 1, f"malformed redirect: {loc}"
        assert loc.startswith("/skills/pdf?version=2.0.0&msg=")
        # Name was inherited from the source, not from the form.
        call = fake.create_skill.call_args[0][0]
        assert call == {
            "id": "pdf",
            "name": "PDF Toolkit",
            "description": "new desc",
            "version": "2.0.0",
            "metadata": {},
            "skillset_ids": [],
        }
        # Bundle copied with same skill id on both sides.
        fake.copy_bundle.assert_awaited_once_with("pdf", "2.0.0", "pdf", "1.0.0")

    def test_upload_bundle_flow(self, webui):
        client, fake = webui
        fake.get_skill.return_value = _base_skill()
        fake.create_skill.return_value = {}
        fake.upload_bundle.return_value = {"file_count": 2, "total_size": 10}
        r = client.post(
            "/skills/pdf/new-version",
            data=self._base_form(bundle_action="upload"),
            files={"file": ("b.zip", b"PK\x03\x04data", "application/zip")},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"].count("?") == 1
        fake.upload_bundle.assert_awaited_once()
        args = fake.upload_bundle.call_args[0]
        assert args[0] == "pdf" and args[1] == "2.0.0" and args[2] == "b.zip"
        fake.copy_bundle.assert_not_called()

    def test_none_bundle_skips_copy_and_upload(self, webui):
        client, fake = webui
        fake.get_skill.return_value = _base_skill()
        fake.create_skill.return_value = {}
        r = client.post(
            "/skills/pdf/new-version",
            data=self._base_form(bundle_action="none"),
            follow_redirects=False,
        )
        assert r.status_code == 303
        fake.copy_bundle.assert_not_called()
        fake.upload_bundle.assert_not_called()

    def test_invalid_metadata_redirects_back_with_clean_url(self, webui):
        client, fake = webui
        fake.get_skill.return_value = _base_skill()
        r = client.post(
            "/skills/pdf/new-version",
            data=self._base_form(metadata="not json"),
            follow_redirects=False,
        )
        assert r.status_code == 303
        loc = r.headers["location"]
        # Regression: one '?' only; handler must not append "?msg=" to a path
        # that already has a query string.
        assert loc.count("?") == 1, f"malformed redirect: {loc}"
        assert loc.startswith("/skills/pdf/new-version?from=1.0.0&msg=")
        fake.create_skill.assert_not_called()

    def test_name_form_field_ignored(self, webui):
        """Client-sent 'name' must never override the source skill's name."""
        client, fake = webui
        fake.get_skill.return_value = _base_skill(name="Original")
        fake.create_skill.return_value = {}
        fake.copy_bundle.return_value = {"file_count": 0, "total_size": 0}
        client.post(
            "/skills/pdf/new-version",
            data={**self._base_form(), "name": "Hacker Override"},
            follow_redirects=False,
        )
        call = fake.create_skill.call_args[0][0]
        assert call["name"] == "Original"

    def test_create_skill_failure_redirects_back(self, webui):
        client, fake = webui
        fake.get_skill.return_value = _base_skill()
        fake.create_skill.side_effect = MCPError("duplicate", status_code=409)
        r = client.post(
            "/skills/pdf/new-version",
            data=self._base_form(),
            follow_redirects=False,
        )
        assert r.status_code == 303
        loc = r.headers["location"]
        assert loc.count("?") == 1
        assert loc.startswith("/skills/pdf/new-version?from=1.0.0&msg=")
        assert "msg_type=error" in loc
        fake.copy_bundle.assert_not_called()

    def test_upload_bundle_failure_still_leaves_version(self, webui):
        """If the bundle upload fails after the version is created, the user
        sees the new version with an error flash, not a total wipe."""
        client, fake = webui
        fake.get_skill.return_value = _base_skill()
        fake.create_skill.return_value = {}
        fake.upload_bundle.side_effect = MCPError("broken archive", status_code=400)
        r = client.post(
            "/skills/pdf/new-version",
            data=self._base_form(bundle_action="upload"),
            files={"file": ("b.zip", b"garbage", "application/zip")},
            follow_redirects=False,
        )
        loc = r.headers["location"]
        assert loc.count("?") == 1
        assert loc.startswith("/skills/pdf?version=2.0.0&")
        assert "msg_type=error" in loc


# ---------------------------------------------------------------------------
# Clone flow
# ---------------------------------------------------------------------------

class TestCloneFlow:
    def _base_form(self, **overrides):
        base = {
            "new_id": "pdf2",
            "new_name": "PDF Toolkit v2",
            "version": "1.0.0",
            "from_version": "1.0.0",
            "bundle_action": "copy",
            "description": "",
            "metadata": "{}",
        }
        base.update(overrides)
        return base

    def test_get_clone_page(self, webui):
        client, fake = webui
        fake.get_skill.return_value = _base_skill()
        fake.list_bundle_files.return_value = [
            {"path": "SKILL.md", "size": 10, "sha256": "x"}
        ]
        r = client.get("/skills/pdf/clone?from=1.0.0")
        assert r.status_code == 200
        assert b"Clone" in r.content
        # "Copy bundle from pdf v1.0.0"
        assert b"v1.0.0" in r.content

    def test_clone_with_copy_calls_cross_skill_copy(self, webui):
        client, fake = webui
        fake.create_skill.return_value = {}
        fake.copy_bundle.return_value = {"file_count": 2, "total_size": 20}
        r = client.post(
            "/skills/pdf/clone",
            data=self._base_form(),
            follow_redirects=False,
        )
        assert r.status_code == 303
        loc = r.headers["location"]
        assert loc.count("?") == 1
        assert loc.startswith("/skills/pdf2?version=1.0.0&msg=")
        # Copy uses different src and dst skill ids.
        fake.copy_bundle.assert_awaited_once_with("pdf2", "1.0.0", "pdf", "1.0.0")
        # Name is whatever the user typed (editable in clone).
        call = fake.create_skill.call_args[0][0]
        assert call["id"] == "pdf2"
        assert call["name"] == "PDF Toolkit v2"

    def test_clone_invalid_metadata_redirects_back(self, webui):
        client, fake = webui
        r = client.post(
            "/skills/pdf/clone",
            data=self._base_form(metadata="{not json"),
            follow_redirects=False,
        )
        loc = r.headers["location"]
        assert loc.count("?") == 1
        assert loc.startswith("/skills/pdf/clone?from=1.0.0&msg=")
        assert "msg_type=error" in loc
        fake.create_skill.assert_not_called()


# ---------------------------------------------------------------------------
# Skill view page (read-only)
# ---------------------------------------------------------------------------

class TestSkillView:
    def test_view_with_version(self, webui):
        client, fake = webui
        fake.get_skill.return_value = _base_skill(version="2.0.0")
        fake.list_skill_versions.return_value = [
            {"version": "1.0.0", "is_latest": False, "created_at": ""},
            {"version": "2.0.0", "is_latest": True, "created_at": ""},
        ]
        r = client.get("/skills/pdf?version=2.0.0")
        assert r.status_code == 200
        body = r.content
        assert b"v2.0.0" in body
        # No edit form (page is read-only now).
        assert b"/skills/pdf/update" not in body
        # New version + Clone buttons are present.
        assert b"New version from v2.0.0" in body
        assert b"Clone" in body

    def test_view_unknown_version_redirects_cleanly(self, webui):
        client, fake = webui
        fake.get_skill.side_effect = MCPError("Skill not found", status_code=404)
        r = client.get("/skills/pdf?version=9.9.9", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"].count("?") == 1


# ---------------------------------------------------------------------------
# Skills list with search + filter
# ---------------------------------------------------------------------------

class TestSkillsList:
    def test_filter_markup_and_membership(self, webui):
        client, fake = webui
        fake.list_skills.return_value = [
            _base_skill(id="pdf", name="PDF"),
            _base_skill(id="docx", name="DOCX"),
        ]
        fake.list_skillsets.return_value = [
            {"id": "anthropic", "name": "Anthropic", "description": "",
             "created_at": "", "updated_at": ""}
        ]
        # Only pdf belongs to anthropic.
        fake.list_skillset_skills.return_value = [
            {"id": "pdf", "name": "PDF", "version": "1.0.0",
             "description": "", "is_latest": True, "metadata": {},
             "created_at": "", "updated_at": ""}
        ]
        r = client.get("/skills")
        assert r.status_code == 200
        body = r.content
        # Search input is there.
        assert b'id="skill-search"' in body
        # Filter pill for the skillset is there.
        assert b'data-skillset-id="anthropic"' in body
        # Membership was resolved server-side and attached to rows.
        assert b'data-skillsets="anthropic"' in body
        # docx has no membership -> empty attr.
        assert b'data-skillsets=""' in body
