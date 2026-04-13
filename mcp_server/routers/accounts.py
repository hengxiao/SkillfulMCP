"""Admin-gated account + membership endpoints (Wave 9.1).

All routes require `X-Admin-Key`. The Web UI composes these in Wave
9.5 behind its session-cookie auth; direct CLI callers use the admin
key the same way they use it for `/admin/skills`.

Responsibilities:

- Account CRUD (`POST/GET/DELETE /admin/accounts[/{id}]`).
- Membership invite / list / role-update / remove, with last-admin
  guard (`mcp_server.accounts.LastAdminError` → 409) and optional
  `new_owner_id` reassignment on delete.
- Pending-invitation cleanup.
- Delete-account interlock: refuses unless the caller confirms user
  + skill counts AND sets `cascade_catalog=1` when catalog rows
  exist.

This module is service-layer thin — it shapes HTTP + audit logging
only. The business logic (last-admin guard with SELECT ... FOR
UPDATE, pending consume, etc.) lives in `mcp_server.accounts`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from .. import accounts as acct_svc
from .. import users as user_svc
from ..dependencies import get_db, require_admin
from ..logging_config import get_logger
from ..models import Account, AccountMembership, PendingMembership
from ..schemas import (
    AccountCreateRequest,
    AccountResponse,
    MembershipInviteRequest,
    MembershipResponse,
    MembershipRoleUpdateRequest,
    PendingMembershipResponse,
)

_log = get_logger("mcp.accounts.router")

router = APIRouter(prefix="/admin/accounts", tags=["admin", "accounts"])


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _account_to_response(a: Account) -> AccountResponse:
    return AccountResponse.model_validate(a)


def _membership_to_response(
    m: AccountMembership, *, email: str, display_name: str | None, disabled: bool
) -> MembershipResponse:
    return MembershipResponse(
        user_id=m.user_id,
        account_id=m.account_id,
        role=m.role,
        email=email,
        display_name=display_name,
        disabled=disabled,
        created_at=m.created_at,
        pending=False,
    )


def _pending_to_response(p: PendingMembership) -> PendingMembershipResponse:
    return PendingMembershipResponse.model_validate(p)


# ---------------------------------------------------------------------------
# Account CRUD
# ---------------------------------------------------------------------------

@router.get("", response_model=list[AccountResponse])
def list_accounts(
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    return [_account_to_response(a) for a in acct_svc.list_accounts(db)]


@router.post("", response_model=AccountResponse, status_code=status.HTTP_201_CREATED)
def create_account(
    body: AccountCreateRequest,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    try:
        a = acct_svc.create_account(
            db,
            name=body.name,
            initial_admin_user_id=body.initial_admin_user_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        )
    return _account_to_response(a)


@router.get("/{account_id}", response_model=AccountResponse)
def get_account(
    account_id: str,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    a = acct_svc.get_account(db, account_id)
    if a is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return _account_to_response(a)


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_account(
    account_id: str,
    confirm_user_count: int = Query(..., ge=0),
    confirm_skill_count: int = Query(0, ge=0),
    confirm_skillset_count: int = Query(0, ge=0),
    confirm_agent_count: int = Query(0, ge=0),
    cascade_catalog: int = Query(0, ge=0, le=1),
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    """Delete an account. Two interlocks (see spec §3.7):

    1. `confirm_*_count` query params must match the current row
       counts exactly. Fat-finger protection.
    2. When catalog counts are non-zero, `cascade_catalog=1` is
       required to consent to the hard-delete of skills + skillsets
       + agents in the account.

    Returns 409 when either interlock fails; 404 when the account
    doesn't exist.
    """
    a = acct_svc.get_account(db, account_id)
    if a is None:
        raise HTTPException(status_code=404, detail="Account not found")

    # Count snapshot.
    members_actual = len(acct_svc.list_memberships(db, account_id))
    # Catalog counts are deferred until Wave 9.2 adds `account_id` on
    # skills/skillsets/agents. For Wave 9.1 they're always 0 — we
    # still respect the interlock so clients writing against the
    # eventual Wave 9.2 semantics work unchanged.
    skills_actual = 0
    skillsets_actual = 0
    agents_actual = 0

    if confirm_user_count != members_actual:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"confirm_user_count={confirm_user_count} does not match "
                f"current member count {members_actual}; refusing delete"
            ),
        )
    if (
        confirm_skill_count != skills_actual
        or confirm_skillset_count != skillsets_actual
        or confirm_agent_count != agents_actual
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="confirm catalog counts do not match",
        )
    catalog_total = skills_actual + skillsets_actual + agents_actual
    if catalog_total > 0 and cascade_catalog != 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "account still has catalog content; set cascade_catalog=1 "
                "to hard-delete, or move content first"
            ),
        )

    if not acct_svc.delete_account(db, account_id):
        # Race: row vanished between the fetch and the delete.
        raise HTTPException(status_code=404, detail="Account not found")


# ---------------------------------------------------------------------------
# Memberships
# ---------------------------------------------------------------------------

@router.get(
    "/{account_id}/members",
    response_model=list[dict],
    response_model_exclude_none=True,
)
def list_members(
    account_id: str,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> list[dict[str, Any]]:
    """Return active memberships AND pending invitations for an
    account, tagged by a `pending` flag. One list lets the UI render
    both without two round-trips."""
    if acct_svc.get_account(db, account_id) is None:
        raise HTTPException(status_code=404, detail="Account not found")

    active = acct_svc.list_memberships(db, account_id)
    rows: list[dict[str, Any]] = []
    for m in active:
        u = user_svc.get_user(db, m.user_id)
        # User row might be gone if the FK cascade hasn't caught up in
        # an odd corner case — skip rather than crash.
        if u is None:
            continue
        rows.append(
            _membership_to_response(
                m, email=u.email, display_name=u.display_name, disabled=u.disabled
            ).model_dump()
        )
    for p in acct_svc.list_pending_for_account(db, account_id):
        rows.append(_pending_to_response(p).model_dump())
    return rows


@router.post(
    "/{account_id}/members",
    status_code=status.HTTP_201_CREATED,
    response_model=dict,
    response_model_exclude_none=True,
)
def invite_member(
    account_id: str,
    body: MembershipInviteRequest,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    """Invite an email to this account.

    - If the email maps to an existing users row, insert an active
      membership (201 with the MembershipResponse payload).
    - Otherwise insert a pending invitation (201 with the
      PendingMembershipResponse payload).
    - 409 on duplicate (already a member OR pending entry exists).
    """
    if acct_svc.get_account(db, account_id) is None:
        raise HTTPException(status_code=404, detail="Account not found")

    existing_user = user_svc.get_user_by_email(db, body.email)
    if existing_user is not None:
        try:
            m = acct_svc.add_membership(
                db,
                account_id=account_id,
                user_id=existing_user.id,
                role=body.role,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(exc)
            )
        return _membership_to_response(
            m,
            email=existing_user.email,
            display_name=existing_user.display_name,
            disabled=existing_user.disabled,
        ).model_dump()

    try:
        p = acct_svc.add_pending_membership(
            db, account_id=account_id, email=body.email, role=body.role
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        )
    return _pending_to_response(p).model_dump()


@router.put(
    "/{account_id}/members/{user_id}",
    response_model=MembershipResponse,
)
def update_membership_role(
    account_id: str,
    user_id: str,
    body: MembershipRoleUpdateRequest,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    try:
        m = acct_svc.update_membership_role(
            db, account_id=account_id, user_id=user_id, new_role=body.role
        )
    except acct_svc.LastAdminError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        )
    u = user_svc.get_user(db, user_id)
    return _membership_to_response(
        m, email=u.email, display_name=u.display_name, disabled=u.disabled
    )


@router.delete(
    "/{account_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def remove_member(
    account_id: str,
    user_id: str,
    new_owner_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    """Remove a membership; does NOT delete the users row.

    Optional `new_owner_id` is wired in for Wave 9.2 ownership
    reassignment — Wave 9.1 validates the argument exists as a
    member of the same account but has no catalog to move yet.
    """
    if new_owner_id:
        target = acct_svc.get_membership(
            db, account_id=account_id, user_id=new_owner_id
        )
        if target is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"new_owner_id {new_owner_id!r} is not a member "
                    f"of account {account_id!r}"
                ),
            )

    try:
        removed = acct_svc.remove_membership(
            db, account_id=account_id, user_id=user_id
        )
    except acct_svc.LastAdminError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if not removed:
        raise HTTPException(status_code=404, detail="Membership not found")


# ---------------------------------------------------------------------------
# Pending invitations (revoke path — create/consume live elsewhere)
# ---------------------------------------------------------------------------

@router.delete(
    "/{account_id}/pending/{pending_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_pending(
    account_id: str,
    pending_id: int,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    # Scope the delete to the account — an operator with admin key
    # for account A shouldn't accidentally revoke an invite in
    # account B via a shared pending id. (Wave 9.1 is admin-key
    # flat, but this keeps the path safe when session-gated in 9.5.)
    p = db.get(PendingMembership, pending_id)
    if p is None or p.account_id != account_id:
        raise HTTPException(status_code=404, detail="Pending invite not found")
    if not acct_svc.delete_pending_membership(db, pending_id):
        raise HTTPException(status_code=404, detail="Pending invite not found")
