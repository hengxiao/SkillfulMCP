"""SMTP invitation tests (item F).

Service layer:
- SMTPConfig.from_env picks up required + optional env vars.
- NullMailer records messages in memory.
- send_invite renders the template and delegates to the mailer.
- Inviting via POST /admin/accounts/{id}/members triggers the
  mailer when SMTP is configured (NullMailer in tests — real
  transport is out of scope for unit tests).
"""

from __future__ import annotations

import pytest

from mcp_server import mailer as mailer_mod

from tests.conftest import ADMIN_HEADERS


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------

class TestSMTPConfig:
    def test_from_env_none_when_host_missing(self, monkeypatch):
        monkeypatch.delenv("MCP_SMTP_HOST", raising=False)
        assert mailer_mod.SMTPConfig.from_env() is None

    def test_from_env_requires_from_addr(self, monkeypatch):
        monkeypatch.setenv("MCP_SMTP_HOST", "smtp.example.com")
        monkeypatch.delenv("MCP_SMTP_FROM", raising=False)
        with pytest.raises(RuntimeError, match="MCP_SMTP_FROM"):
            mailer_mod.SMTPConfig.from_env()

    def test_from_env_parses_defaults(self, monkeypatch):
        monkeypatch.setenv("MCP_SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("MCP_SMTP_FROM", "noreply@example.com")
        cfg = mailer_mod.SMTPConfig.from_env()
        assert cfg.host == "smtp.example.com"
        assert cfg.port == 587
        assert cfg.use_tls is True
        assert cfg.username is None

    def test_from_env_respects_tls_off(self, monkeypatch):
        monkeypatch.setenv("MCP_SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("MCP_SMTP_FROM", "x@y.com")
        monkeypatch.setenv("MCP_SMTP_TLS", "0")
        cfg = mailer_mod.SMTPConfig.from_env()
        assert cfg.use_tls is False


# ---------------------------------------------------------------------------
# NullMailer + send_invite template
# ---------------------------------------------------------------------------

class TestInviteRendering:
    def test_null_mailer_records_message(self):
        null = mailer_mod.NullMailer()
        null.send(to="a@b.com", subject="hi", body="hello")
        assert null.sent == [
            {"to": "a@b.com", "subject": "hi", "body": "hello"}
        ]

    def test_send_invite_uses_template(self):
        null = mailer_mod.NullMailer()
        mailer_mod.send_invite(
            to="alice@partner.com",
            account_name="Corp Ops",
            role="contributor",
            inviter="bob@corp.com",
            signup_url="https://example.test/signup",
            mailer=null,
        )
        assert len(null.sent) == 1
        msg = null.sent[0]
        assert msg["to"] == "alice@partner.com"
        assert "Corp Ops" in msg["subject"]
        body = msg["body"]
        assert "bob@corp.com" in body
        assert "contributor" in body
        assert "https://example.test/signup" in body


# ---------------------------------------------------------------------------
# End-to-end: invite API triggers the mailer
# ---------------------------------------------------------------------------

class TestInviteEndpointSendsMail:
    def test_unknown_user_invite_emails_grantee(self, client, monkeypatch):
        # Use the NullMailer the conftest fixture already installed.
        null = mailer_mod.get_default_mailer()
        assert isinstance(null, mailer_mod.NullMailer)

        monkeypatch.setenv("MCP_WEBUI_PUBLIC_URL", "https://mcp.test")

        u = client.post(
            "/admin/users",
            json={"email": "ops@x.com", "password": "s3cret-pass"},
            headers=ADMIN_HEADERS,
        ).json()
        a = client.post(
            "/admin/accounts",
            json={"name": "Emailing", "initial_admin_user_id": u["id"]},
            headers=ADMIN_HEADERS,
        ).json()
        r = client.post(
            f"/admin/accounts/{a['id']}/members",
            json={"email": "newhire@corp.com", "role": "contributor"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 201, r.text
        assert len(null.sent) == 1
        msg = null.sent[0]
        assert msg["to"] == "newhire@corp.com"
        assert "Emailing" in msg["body"]
        assert "https://mcp.test/signup" in msg["body"]

    def test_existing_user_invite_does_not_email(self, client):
        """An existing-user invite creates an active membership; no
        invite email — they can just log in."""
        null = mailer_mod.get_default_mailer()
        u = client.post(
            "/admin/users",
            json={"email": "ops2@x.com", "password": "s3cret-pass"},
            headers=ADMIN_HEADERS,
        ).json()
        # Pre-create the invited user so the invite is NOT pending.
        client.post(
            "/admin/users",
            json={"email": "existing@x.com", "password": "s3cret-pass"},
            headers=ADMIN_HEADERS,
        )
        a = client.post(
            "/admin/accounts",
            json={"name": "Existing", "initial_admin_user_id": u["id"]},
            headers=ADMIN_HEADERS,
        ).json()
        before = len(null.sent)
        client.post(
            f"/admin/accounts/{a['id']}/members",
            json={"email": "existing@x.com", "role": "viewer"},
            headers=ADMIN_HEADERS,
        )
        assert len(null.sent) == before
