"""CLI subcommand tests — `mcp-cli superadmin *` (item K).

`hash-password` delegates to mcp_server.pwhash.hash_password.
`rotate` edits a .env-like file in place; dry-run must not touch
the file.
`show-email` prints the hardcoded pseudo-email.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from cli.main import app
from mcp_server.pwhash import verify_password
from mcp_server.users import SUPERADMIN_EMAIL


runner = CliRunner()


class TestShowEmail:
    def test_prints_hardcoded_value(self):
        result = runner.invoke(app, ["superadmin", "show-email"])
        assert result.exit_code == 0
        assert SUPERADMIN_EMAIL in result.stdout


class TestHashPassword:
    def test_noninteractive_prints_hash(self):
        result = runner.invoke(
            app, ["superadmin", "hash-password", "--password", "rotate-me"]
        )
        assert result.exit_code == 0, result.stdout
        hashed = result.stdout.strip().splitlines()[-1]
        assert verify_password("rotate-me", hashed)


class TestRotate:
    def test_updates_existing_env_line(self, tmp_path: Path):
        env = tmp_path / ".env"
        env.write_text(
            "MCP_JWT_SECRET=abc\n"
            "MCP_SUPERADMIN_PASSWORD_HASH=old-value\n"
            "MCP_OTHER=x\n"
        )
        result = runner.invoke(
            app,
            [
                "superadmin", "rotate",
                "--password", "new-secret",
                "--env-file", str(env),
            ],
        )
        assert result.exit_code == 0, result.stdout
        body = env.read_text()
        # Other lines preserved.
        assert "MCP_JWT_SECRET=abc" in body
        assert "MCP_OTHER=x" in body
        # Old hash is gone.
        assert "MCP_SUPERADMIN_PASSWORD_HASH=old-value" not in body
        # New hash verifies against the new password.
        for line in body.splitlines():
            if line.startswith("MCP_SUPERADMIN_PASSWORD_HASH="):
                hashed = line.split("=", 1)[1]
                assert verify_password("new-secret", hashed)
                break
        else:
            raise AssertionError("MCP_SUPERADMIN_PASSWORD_HASH not found")

    def test_appends_when_absent(self, tmp_path: Path):
        env = tmp_path / ".env"
        env.write_text("MCP_JWT_SECRET=abc\n")
        result = runner.invoke(
            app,
            [
                "superadmin", "rotate",
                "--password", "new-secret",
                "--env-file", str(env),
            ],
        )
        assert result.exit_code == 0, result.stdout
        body = env.read_text()
        # Original line kept; new one appended.
        assert body.count("MCP_SUPERADMIN_PASSWORD_HASH=") == 1

    def test_dry_run_does_not_write(self, tmp_path: Path):
        env = tmp_path / ".env"
        env.write_text("MCP_SUPERADMIN_PASSWORD_HASH=pinned\n")
        result = runner.invoke(
            app,
            [
                "superadmin", "rotate",
                "--password", "anything",
                "--env-file", str(env),
                "--dry-run",
            ],
        )
        assert result.exit_code == 0
        # File untouched.
        assert env.read_text() == "MCP_SUPERADMIN_PASSWORD_HASH=pinned\n"
        assert "DRY RUN" in result.stdout

    def test_creates_missing_env_file(self, tmp_path: Path):
        env = tmp_path / "fresh.env"
        assert not env.exists()
        result = runner.invoke(
            app,
            [
                "superadmin", "rotate",
                "--password", "new-secret",
                "--env-file", str(env),
            ],
        )
        assert result.exit_code == 0, result.stdout
        assert env.exists()
        assert "MCP_SUPERADMIN_PASSWORD_HASH=" in env.read_text()
