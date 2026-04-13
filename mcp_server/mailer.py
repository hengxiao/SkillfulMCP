"""SMTP mailer (item F).

Thin wrapper around `smtplib` with a pluggable transport so tests
can inject an in-memory `Mailer` and production code can use the
real SMTP path without a monkey-patch dance.

Env configuration:
  MCP_SMTP_HOST          required to enable outbound mail
  MCP_SMTP_PORT          default 587
  MCP_SMTP_USER          optional
  MCP_SMTP_PASSWORD      optional
  MCP_SMTP_FROM          required when host is set
  MCP_SMTP_TLS           "1" | "0"; default "1" (STARTTLS)

When MCP_SMTP_HOST is unset, :func:`get_default_mailer` returns a
:class:`NullMailer` that silently no-ops. Callers render the
signup link into the return value so the Web UI can show a
"copy invite link" fallback when SMTP isn't configured.
"""

from __future__ import annotations

import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

from .logging_config import get_logger

_log = get_logger("mcp.mailer")


@dataclass(frozen=True)
class SMTPConfig:
    host: str
    port: int
    username: str | None
    password: str | None
    from_addr: str
    use_tls: bool

    @classmethod
    def from_env(cls) -> "SMTPConfig | None":
        host = os.environ.get("MCP_SMTP_HOST", "").strip()
        if not host:
            return None
        from_addr = os.environ.get("MCP_SMTP_FROM", "").strip()
        if not from_addr:
            raise RuntimeError(
                "MCP_SMTP_HOST is set but MCP_SMTP_FROM is empty — "
                "cannot send outbound mail"
            )
        return cls(
            host=host,
            port=int(os.environ.get("MCP_SMTP_PORT", "587")),
            username=(os.environ.get("MCP_SMTP_USER") or None),
            password=(os.environ.get("MCP_SMTP_PASSWORD") or None),
            from_addr=from_addr,
            use_tls=os.environ.get("MCP_SMTP_TLS", "1").strip() != "0",
        )


class Mailer(Protocol):
    def send(self, *, to: str, subject: str, body: str) -> None: ...


class NullMailer:
    """No-op mailer used when SMTP isn't configured.

    Records the last message in-process so tests (and deployments
    using the copy-link fallback) can introspect what would have
    been sent.
    """

    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    def send(self, *, to: str, subject: str, body: str) -> None:
        self.sent.append({"to": to, "subject": subject, "body": body})
        _log.info(
            "mail.noop",
            extra={"to": to, "subject": subject, "length": len(body)},
        )


class SMTPMailer:
    def __init__(self, cfg: SMTPConfig) -> None:
        self.cfg = cfg

    def send(self, *, to: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"] = self.cfg.from_addr
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)

        cfg = self.cfg
        try:
            with smtplib.SMTP(cfg.host, cfg.port, timeout=10) as server:
                server.ehlo()
                if cfg.use_tls:
                    server.starttls()
                    server.ehlo()
                if cfg.username and cfg.password:
                    server.login(cfg.username, cfg.password)
                server.send_message(msg)
            _log.info(
                "mail.sent", extra={"to": to, "subject": subject}
            )
        except Exception as exc:
            # Don't propagate — mail failures should be surfaced via
            # the copy-link fallback in the Web UI, not crash the
            # calling request.
            _log.exception(
                "mail.send_failed",
                extra={"to": to, "subject": subject, "error": str(exc)},
            )


# ---------------------------------------------------------------------------
# Default-instance singleton
# ---------------------------------------------------------------------------

_default_mailer: Mailer | None = None


def get_default_mailer() -> Mailer:
    global _default_mailer
    if _default_mailer is None:
        cfg = SMTPConfig.from_env()
        _default_mailer = SMTPMailer(cfg) if cfg else NullMailer()
    return _default_mailer


def set_default_mailer(mailer: Mailer) -> None:
    """Test hook."""
    global _default_mailer
    _default_mailer = mailer


def reset_default_mailer() -> None:
    global _default_mailer
    _default_mailer = None


# ---------------------------------------------------------------------------
# Invite message rendering
# ---------------------------------------------------------------------------

_INVITE_TEMPLATE = """Hi,

{inviter} invited you to join the "{account_name}" workspace on
SkillfulMCP as a {role}.

To accept, sign up at:

  {signup_url}

This invitation is tied to your email ({to}). Once you sign up
with this address, the invitation resolves automatically and you
land in the workspace on your next login.

If you weren't expecting this invitation, you can ignore this
email — the pending entry expires when an account admin revokes
it.
"""


def send_invite(
    *,
    to: str,
    account_name: str,
    role: str,
    inviter: str,
    signup_url: str,
    mailer: Mailer | None = None,
) -> None:
    """Render + send the invite body. Safe no-op when SMTP isn't
    configured (NullMailer). The caller is still responsible for
    persisting the pending_memberships row — this is delivery only.
    """
    m = mailer or get_default_mailer()
    body = _INVITE_TEMPLATE.format(
        to=to, account_name=account_name, role=role,
        inviter=inviter, signup_url=signup_url,
    )
    m.send(
        to=to,
        subject=f"You're invited to join {account_name} on SkillfulMCP",
        body=body,
    )
