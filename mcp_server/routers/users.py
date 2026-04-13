"""Admin-gated user identity endpoints.

Wave 9 keeps this router alive for two reasons:

1. `POST /admin/users/authenticate` is still the Web UI's login path
   — now with an env-hardcoded superadmin bypass (spec §2.3).
2. `GET /admin/users` + CRUD stays available for the Wave 8b
   admin-managed user pages until Wave 9.5 replaces them with the
   account-scoped subtree flow.

Role management moved out of this router entirely — there is no
`role` on a Wave 9 `users` row. Account-scoped roles live on
`account_memberships` and are managed under `mcp_server.accounts`
(Wave 9.1 adds HTTP endpoints for them).
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .. import users as user_svc
from ..dependencies import get_db, require_admin
from ..logging_config import get_logger
from ..models import User
from ..pwhash import hash_password, verify_password
from ..schemas import (
    AuthenticateResponse,
    UserAuthenticateRequest,
    UserCreate,
    UserResponse,
    UserUpdate,
)
from ..users import SUPERADMIN_EMAIL, SUPERADMIN_USER_ID, normalize_email

_log = get_logger("mcp.users.router")

router = APIRouter(prefix="/admin/users", tags=["admin", "users"])


def _to_response(u: User) -> UserResponse:
    return UserResponse.model_validate(u)


@router.get("", response_model=list[UserResponse])
def list_users(
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    return [_to_response(u) for u in user_svc.list_users(db)]


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def create_user(
    body: UserCreate,
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
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return _to_response(u)


@router.get("/{user_id}", response_model=UserResponse)
def get_user(
    user_id: str,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    u = user_svc.get_user(db, user_id)
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    return _to_response(u)


@router.put("/{user_id}", response_model=UserResponse)
def update_user(
    user_id: str,
    body: UserUpdate,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    pw_hash = hash_password(body.password) if body.password else None
    try:
        u = user_svc.update_user(
            db,
            user_id,
            display_name=body.display_name,
            disabled=body.disabled,
            password_hash=pw_hash,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    return _to_response(u)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_id: str,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    if not user_svc.delete_user(db, user_id):
        raise HTTPException(status_code=404, detail="User not found")


@router.post("/authenticate", response_model=AuthenticateResponse)
def authenticate_user(
    body: UserAuthenticateRequest,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    """Verify (email, password). 401 on invalid or disabled.

    Wave 9: the hardcoded superadmin identity (§2.3) is checked first,
    bypassing the DB. When `MCP_SUPERADMIN_PASSWORD_HASH` is set and
    the normalized email matches the reserved pseudo-email, a
    superadmin-flagged response is returned with `id="0"`.
    """
    normalized = normalize_email(body.email)

    # Superadmin shortcut.
    if normalized == SUPERADMIN_EMAIL:
        env_hash = os.environ.get("MCP_SUPERADMIN_PASSWORD_HASH", "")
        if not env_hash:
            # Refuse to log in without a configured hash — ops error,
            # not a guessable state worth distinguishing from 401.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password",
            )
        if not verify_password(body.password, env_hash):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password",
            )
        _log.info(
            "superadmin login",
            extra={"actor_email": SUPERADMIN_EMAIL},
        )
        return AuthenticateResponse(
            id=SUPERADMIN_USER_ID,
            email=SUPERADMIN_EMAIL,
            display_name="Superadmin",
            disabled=False,
            is_superadmin=True,
        )

    # Regular DB-backed user.
    u = user_svc.get_user_by_email(db, normalized)
    if u is None or u.disabled or not verify_password(body.password, u.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )
    user_svc.touch_login(db, u.id)
    return AuthenticateResponse(
        id=u.id,
        email=u.email,
        display_name=u.display_name,
        disabled=u.disabled,
        is_superadmin=False,
    )
