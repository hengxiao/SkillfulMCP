"""Signup endpoint (Wave 9.1).

Mounted at `POST /admin/signup`. The Web UI's public `/signup` form
(Wave 9.5) does its own rate-limiting + invite-gating and proxies the
submission here with the admin key. Direct admin-key callers can also
use this to create users from CLI tooling.

Responsibilities:

- Hash the password server-side so bcrypt never crosses the wire.
- Create the `users` row with the reserved-email guard.
- In the same transaction, consume every `pending_memberships` row
  whose email matches the new user — each becomes a real
  `account_memberships` row. See §3.5.1.
- Return the new user's id, the consumed account ids (for the caller
  to redirect into the right active account), and a structured log
  line capturing the invitations resolved.

The `MCP_ALLOW_PUBLIC_SIGNUP` gate + the superadmin bypass are
enforced by the Web UI layer — this endpoint is already admin-key
gated so an additional flag here would be defense in depth but not
materially different.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .. import accounts as acct_svc
from .. import users as user_svc
from ..dependencies import get_db, require_admin
from ..logging_config import get_logger
from ..pwhash import hash_password
from ..schemas import SignupRequest, SignupResponse

_log = get_logger("mcp.signup.router")

router = APIRouter(tags=["admin", "signup"])


@router.post(
    "/admin/signup",
    response_model=SignupResponse,
    status_code=status.HTTP_201_CREATED,
)
def signup(
    body: SignupRequest,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    try:
        u = user_svc.create_user(
            db,
            email=body.email,
            password_hash=hash_password(body.password),
            display_name=body.display_name,
        )
    except ValueError as exc:
        # reserved-email refusal or duplicate email both surface as
        # 400/409 depending on message. Keep the distinction so the
        # webui can render the right friendly error.
        msg = str(exc)
        code = (
            status.HTTP_400_BAD_REQUEST
            if "reserved" in msg or "required" in msg
            else status.HTTP_409_CONFLICT
        )
        raise HTTPException(status_code=code, detail=msg)

    # Consume pending invitations for this email atomically.
    memberships = acct_svc.consume_pending_for_user(
        db, user_id=u.id, email=u.email
    )
    consumed_ids = [m.account_id for m in memberships]

    # Stamp `last_active_account_id` to the first resolved account so
    # the Web UI has a natural landing target on first login.
    if consumed_ids:
        user_svc.update_user(
            db, u.id, last_active_account_id=consumed_ids[0]
        )

    _log.info(
        "user.signup",
        extra={
            "user_id": u.id,
            "email": u.email,
            "invited_memberships": consumed_ids,
        },
    )
    return SignupResponse(
        id=u.id,
        email=u.email,
        display_name=u.display_name,
        consumed_account_ids=consumed_ids,
    )
