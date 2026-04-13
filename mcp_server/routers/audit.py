"""Admin-gated audit log listing (item H).

`GET /admin/audit[?account_id=<id>&limit=<n>]` returns the most
recent events newest-first. Limit capped at 500 (the service layer
enforces the ceiling; specifying higher silently clamps).

Writing events is the job of `mcp_server.audit.record()`; the HTTP
surface is read-only.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import audit as audit_svc
from ..dependencies import get_db, require_admin
from ..schemas import AuditEventResponse

router = APIRouter(prefix="/admin", tags=["admin", "audit"])


@router.get("/audit", response_model=list[AuditEventResponse])
def list_audit(
    account_id: str | None = Query(
        default=None,
        description="Narrow to events scoped to this account.",
    ),
    limit: int = Query(
        default=100, ge=1, le=500,
        description="Max rows returned, newest-first.",
    ),
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    rows = audit_svc.list_events(db, account_id=account_id, limit=limit)
    return [AuditEventResponse.model_validate(r) for r in rows]
