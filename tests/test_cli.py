"""
Tests for cli/main.py — the `mcp-cli` Typer app.

Strategy: rather than pointing the CLI at a live server, we install an
`httpx.MockTransport` via monkeypatching so every command exercises the
real CLI → httpx → transport → response path. The CLI's own parsing,
error handling, and payload shaping are covered; only the wire itself
is stubbed.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from cli.main import app


# ---------------------------------------------------------------------------
# Mock transport helpers
# ---------------------------------------------------------------------------

class Recorder:
    """Collects every HTTP call the CLI makes so assertions can inspect them."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def handler(self, responses: dict[tuple[str, str], tuple[int, dict | str]]):
        """Return an httpx handler that maps (method, path) → (status, body).

        Paths are matched exactly. Body can be a dict (JSON-encoded) or
        a raw string. Any unregistered call raises loudly so tests fail
        fast.
        """

        def _handle(request: httpx.Request) -> httpx.Response:
            key = (request.method, request.url.path)
            self.calls.append({
                "method": request.method,
                "path": request.url.path,
                "headers": dict(request.headers),
                "content": request.content.decode() if request.content else "",
                "params": dict(request.url.params),
            })
            if key not in responses:
                return httpx.Response(
                    status_code=500,
                    json={"detail": f"unmocked {key!r}"},
                )
            status, body = responses[key]
            if isinstance(body, dict):
                return httpx.Response(status_code=status, json=body)
            return httpx.Response(status_code=status, content=body)

        return _handle


@pytest.fixture()
def runner():
    # Recent Click versions dropped `mix_stderr`; stderr is now always
    # captured separately via `result.stderr`.
    return CliRunner()


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("MCP_SERVER_URL", "http://mock-catalog")
    monkeypatch.setenv("MCP_ADMIN_KEY", "test-admin-key")


@pytest.fixture()
def mock_http(monkeypatch, env):
    """Patch httpx.Client so every CLI invocation uses a MockTransport.

    The test gets back a Recorder; configure its responses per test.
    """
    rec = Recorder()
    real_client = httpx.Client

    def fake_client(*args, base_url=None, **kwargs):
        responses = getattr(fake_client, "_responses", {})
        transport = httpx.MockTransport(rec.handler(responses))
        kwargs.pop("transport", None)
        return real_client(
            base_url=base_url or "http://mock-catalog",
            transport=transport,
            **kwargs,
        )

    fake_client._responses = {}
    monkeypatch.setattr(httpx, "Client", fake_client)
    return rec, fake_client


# ---------------------------------------------------------------------------
# Help / no-args behavior
# ---------------------------------------------------------------------------

class TestHelp:
    def test_root_help(self, runner):
        r = runner.invoke(app, ["--help"])
        assert r.exit_code == 0
        assert "skill" in r.stdout
        assert "agent" in r.stdout
        assert "token" in r.stdout
        assert "catalog" in r.stdout

    def test_no_args_shows_help(self, runner):
        r = runner.invoke(app, [])
        # Typer with no_args_is_help=True returns 2 and prints help.
        assert r.exit_code == 2
        assert "Usage" in r.stdout or "Usage" in r.stderr


# ---------------------------------------------------------------------------
# skill add / delete
# ---------------------------------------------------------------------------

class TestSkillAdd:
    def test_creates_new_skill(self, runner, mock_http):
        rec, client = mock_http
        client._responses = {
            ("POST", "/skills"): (201, {"id": "s1", "version": "1.0.0"}),
        }
        r = runner.invoke(app, [
            "skill", "add", "--id", "s1", "--name", "S1", "--version", "1.0.0",
        ])
        assert r.exit_code == 0, r.stderr
        assert "saved" in r.stdout
        assert len(rec.calls) == 1
        assert rec.calls[0]["method"] == "POST"
        assert rec.calls[0]["path"] == "/skills"
        assert rec.calls[0]["headers"]["x-admin-key"] == "test-admin-key"
        payload = json.loads(rec.calls[0]["content"])
        assert payload["id"] == "s1"
        assert payload["version"] == "1.0.0"
        assert payload["metadata"] == {}

    def test_upgrades_to_upsert_on_409(self, runner, mock_http):
        """409 on POST triggers the PUT fallback path."""
        rec, client = mock_http
        client._responses = {
            ("POST", "/skills"): (409, {"detail": "exists"}),
            ("PUT", "/skills/s1"): (200, {"id": "s1"}),
        }
        r = runner.invoke(app, [
            "skill", "add", "--id", "s1", "--name", "S1", "--version", "1.0.0",
        ])
        assert r.exit_code == 0, r.stderr
        # POST then PUT.
        assert [c["method"] for c in rec.calls] == ["POST", "PUT"]
        # The PUT body drops `id` and `skillset_ids` (those don't belong in upsert).
        put_body = json.loads(rec.calls[1]["content"])
        assert "id" not in put_body
        assert "skillset_ids" not in put_body

    def test_with_skillset_and_metadata(self, runner, mock_http):
        rec, client = mock_http
        client._responses = {("POST", "/skills"): (201, {"id": "s1"})}
        r = runner.invoke(app, [
            "skill", "add",
            "--id", "s1", "--name", "S1", "--version", "1.0.0",
            "--skillset", "billing",
            "--metadata", '{"input_schema":{"type":"object"}}',
        ])
        assert r.exit_code == 0
        body = json.loads(rec.calls[0]["content"])
        assert body["skillset_ids"] == ["billing"]
        assert body["metadata"] == {"input_schema": {"type": "object"}}

    def test_error_response_exits_nonzero(self, runner, mock_http):
        rec, client = mock_http
        client._responses = {("POST", "/skills"): (422, {"detail": "invalid version"})}
        r = runner.invoke(app, [
            "skill", "add", "--id", "s1", "--name", "S1", "--version", "not-semver",
        ])
        assert r.exit_code == 1
        assert "invalid version" in r.stderr

    def test_missing_required_flag(self, runner, env):
        # Without mock_http: the missing --version must be caught before any
        # HTTP call.
        r = runner.invoke(app, ["skill", "add", "--id", "s1", "--name", "S1"])
        assert r.exit_code != 0


class TestSkillDelete:
    def test_delete_all_versions(self, runner, mock_http):
        rec, client = mock_http
        client._responses = {("DELETE", "/skills/s1"): (204, "")}
        r = runner.invoke(app, ["skill", "delete", "--id", "s1"])
        assert r.exit_code == 0
        assert rec.calls[0]["params"] == {}

    def test_delete_specific_version(self, runner, mock_http):
        rec, client = mock_http
        client._responses = {("DELETE", "/skills/s1"): (204, "")}
        r = runner.invoke(app, [
            "skill", "delete", "--id", "s1", "--version", "1.0.0",
        ])
        assert r.exit_code == 0
        assert rec.calls[0]["params"]["version"] == "1.0.0"


# ---------------------------------------------------------------------------
# agent add / delete
# ---------------------------------------------------------------------------

class TestAgentAdd:
    def test_creates_agent(self, runner, mock_http):
        rec, client = mock_http
        client._responses = {("POST", "/agents"): (201, {"id": "a1"})}
        r = runner.invoke(app, [
            "agent", "add",
            "--id", "a1", "--name", "A1",
            "--skillsets", "billing,support",
            "--skills", "lookup-invoice",
            "--scope", "read,execute",
        ])
        assert r.exit_code == 0
        body = json.loads(rec.calls[0]["content"])
        assert body["skillsets"] == ["billing", "support"]
        assert body["skills"] == ["lookup-invoice"]
        assert body["scope"] == ["read", "execute"]

    def test_scope_defaults_to_read(self, runner, mock_http):
        rec, client = mock_http
        client._responses = {("POST", "/agents"): (201, {"id": "a1"})}
        r = runner.invoke(app, ["agent", "add", "--id", "a1", "--name", "A1"])
        assert r.exit_code == 0
        body = json.loads(rec.calls[0]["content"])
        assert body["scope"] == ["read"]
        assert body["skillsets"] == []
        assert body["skills"] == []

    def test_409_upserts_via_put(self, runner, mock_http):
        rec, client = mock_http
        client._responses = {
            ("POST", "/agents"): (409, {"detail": "exists"}),
            ("PUT", "/agents/a1"): (200, {"id": "a1"}),
        }
        r = runner.invoke(app, ["agent", "add", "--id", "a1", "--name", "A1"])
        assert r.exit_code == 0
        assert [c["method"] for c in rec.calls] == ["POST", "PUT"]


class TestAgentDelete:
    def test_delete(self, runner, mock_http):
        rec, client = mock_http
        client._responses = {("DELETE", "/agents/a1"): (204, "")}
        r = runner.invoke(app, ["agent", "delete", "--id", "a1"])
        assert r.exit_code == 0


# ---------------------------------------------------------------------------
# token issue
# ---------------------------------------------------------------------------

class TestTokenIssue:
    def test_prints_access_token_to_stdout(self, runner, mock_http):
        rec, client = mock_http
        client._responses = {
            ("POST", "/token"): (200, {"access_token": "header.payload.sig"}),
        }
        r = runner.invoke(app, ["token", "issue", "--agent-id", "a1"])
        assert r.exit_code == 0
        # Just the token on stdout — designed for shell composition.
        assert r.stdout.strip() == "header.payload.sig"

    def test_custom_expires_in(self, runner, mock_http):
        rec, client = mock_http
        client._responses = {
            ("POST", "/token"): (200, {"access_token": "t"}),
        }
        r = runner.invoke(app, [
            "token", "issue", "--agent-id", "a1", "--expires-in", "60",
        ])
        assert r.exit_code == 0
        body = json.loads(rec.calls[0]["content"])
        assert body["expires_in"] == 60

    def test_404_on_unknown_agent_exits_nonzero(self, runner, mock_http):
        rec, client = mock_http
        client._responses = {
            ("POST", "/token"): (404, {"detail": "Agent 'missing' not found"}),
        }
        r = runner.invoke(app, ["token", "issue", "--agent-id", "missing"])
        assert r.exit_code == 1
        assert "Agent 'missing' not found" in r.stderr


# ---------------------------------------------------------------------------
# catalog import
# ---------------------------------------------------------------------------

def _write(tmp_path: Path, name: str, data: dict) -> Path:
    """Serialize `data` to YAML or JSON based on suffix."""
    p = tmp_path / name
    if name.endswith((".yaml", ".yml")):
        import yaml
        p.write_text(yaml.safe_dump(data), encoding="utf-8")
    else:
        p.write_text(json.dumps(data), encoding="utf-8")
    return p


class TestCatalogImport:
    CATALOG = {
        "skillsets": [{"id": "billing", "name": "Billing"}],
        "skills": [
            {
                "id": "lookup-invoice", "name": "Lookup",
                "version": "1.0.0", "metadata": {},
                "skillset_ids": ["billing"],
            }
        ],
        "agents": [
            {
                "id": "a1", "name": "A1",
                "skillsets": ["billing"], "skills": [], "scope": ["read"],
            }
        ],
    }

    def test_yaml_import(self, runner, mock_http, tmp_path):
        rec, client = mock_http
        client._responses = {
            ("POST", "/skillsets"): (201, {"id": "billing"}),
            ("POST", "/skills"):    (201, {"id": "lookup-invoice"}),
            ("POST", "/agents"):    (201, {"id": "a1"}),
        }
        f = _write(tmp_path, "catalog.yaml", self.CATALOG)
        r = runner.invoke(app, ["catalog", "import", "--file", str(f)])
        assert r.exit_code == 0, r.stderr
        assert "1 skillsets, 1 skills, 1 agents" in r.stdout
        # Called in the documented order: skillsets → skills → agents.
        assert [c["method"] + c["path"] for c in rec.calls] == [
            "POST/skillsets", "POST/skills", "POST/agents",
        ]

    def test_json_import(self, runner, mock_http, tmp_path):
        rec, client = mock_http
        client._responses = {
            ("POST", "/skillsets"): (201, {}),
            ("POST", "/skills"):    (201, {}),
            ("POST", "/agents"):    (201, {}),
        }
        f = _write(tmp_path, "catalog.json", self.CATALOG)
        r = runner.invoke(app, ["catalog", "import", "--file", str(f)])
        assert r.exit_code == 0

    def test_conflict_without_upsert_aborts(self, runner, mock_http, tmp_path):
        rec, client = mock_http
        client._responses = {
            ("POST", "/skillsets"): (409, {"detail": "exists"}),
        }
        f = _write(tmp_path, "catalog.yaml", self.CATALOG)
        r = runner.invoke(app, ["catalog", "import", "--file", str(f)])
        assert r.exit_code == 1
        # Only the first call was made; nothing after the 409.
        assert len(rec.calls) == 1

    def test_conflict_with_upsert_falls_back_to_put(self, runner, mock_http, tmp_path):
        rec, client = mock_http
        client._responses = {
            ("POST", "/skillsets"):         (409, {"detail": "exists"}),
            ("PUT",  "/skillsets/billing"): (200, {}),
            ("POST", "/skills"):            (201, {}),
            ("POST", "/agents"):            (201, {}),
        }
        f = _write(tmp_path, "catalog.yaml", self.CATALOG)
        r = runner.invoke(app, [
            "catalog", "import", "--file", str(f), "--upsert",
        ])
        assert r.exit_code == 0
        assert [c["method"] for c in rec.calls] == ["POST", "PUT", "POST", "POST"]


# ---------------------------------------------------------------------------
# Error routing
# ---------------------------------------------------------------------------

class TestErrorHandling:
    def test_non_json_error_body_preserved(self, runner, mock_http):
        rec, client = mock_http
        client._responses = {
            ("POST", "/skills"): (500, "Internal error - not JSON"),
        }
        r = runner.invoke(app, [
            "skill", "add", "--id", "s1", "--name", "S1", "--version", "1.0.0",
        ])
        assert r.exit_code == 1
        assert "Internal error - not JSON" in r.stderr

    def test_admin_key_forwarded_on_every_write(self, runner, mock_http):
        rec, client = mock_http
        client._responses = {
            ("POST", "/skills"): (201, {}),
            ("DELETE", "/skills/s1"): (204, ""),
        }
        runner.invoke(app, ["skill", "add", "--id", "s1", "--name", "S", "--version", "1.0.0"])
        runner.invoke(app, ["skill", "delete", "--id", "s1"])
        # Both calls carry the admin key.
        for c in rec.calls:
            assert c["headers"].get("x-admin-key") == "test-admin-key"
