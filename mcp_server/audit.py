"""Audit event recorder (item H).

One public entry point: :func:`record`. Callers pass what they
know; missing fields are OK. The function wraps everything in a
try/except so audit-log write failures never break the primary
operation — a broken audit path is worse operationally than losing
one row.

Structured JSON logging still happens alongside the DB write so
Loki / Datadog users keep the stream they're grepping today.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy.orm import Session

from .logging_config import get_logger
from .models import AuditEvent

_log = get_logger("mcp.audit")


def record(
    db: Session,
    *,
    action: str,
    actor_email: str | None = None,
    actor_user_id: str | None = None,
    account_id: str | None = None,
    target_kind: str | None = None,
    target_id: str | None = None,
    diff: Mapping[str, Any] | None = None,
) -> None:
    """Append a row to `audit_events` + emit a structured log line.

    The DB write commits immediately so the row survives even if
    the calling request is later rolled back — audit is load-
    bearing; losing a row because a business transaction failed
    defeats the purpose.
    """
    try:
        event = AuditEvent(
            ts=datetime.now(timezone.utc),
            actor_email=actor_email,
            actor_user_id=actor_user_id,
            action=action,
            account_id=account_id,
            target_kind=target_kind,
            target_id=target_id,
            diff=dict(diff) if diff is not None else None,
        )
        db.add(event)
        db.commit()
    except Exception as exc:  # pragma: no cover - defensive
        # Never let an audit failure break the primary operation.
        db.rollback()
        _log.exception(
            "audit.record_failed",
            extra={"action": action, "error": str(exc)},
        )
        return

    _log.info(
        "audit",
        extra={
            "action": action,
            "actor_email": actor_email,
            "actor_user_id": actor_user_id,
            "account_id": account_id,
            "target_kind": target_kind,
            "target_id": target_id,
            "diff": event.diff,
        },
    )


def list_events(
    db: Session,
    *,
    account_id: str | None = None,
    limit: int = 100,
) -> list[AuditEvent]:
    """Newest-first listing, optionally scoped to an account."""
    q = db.query(AuditEvent).order_by(AuditEvent.ts.desc())
    if account_id is not None:
        q = q.filter(AuditEvent.account_id == account_id)
    return q.limit(max(1, min(int(limit), 500))).all()
