"""Admin-only CRUD endpoints for Web UI operator accounts (Wave 8b).

All routes require `X-Admin-Key`. The Web UI hits these to list/create/
update/delete users and to verify credentials at login time.

Authentication is intentionally centralized here (rather than shipping
password hashes over the wire) so the bcrypt library only has to live in
one place.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .. import users as user_svc
from ..dependencies import get_db, require_admin
from ..logging_config import get_logger
from ..models import User
from ..pwhash import hash_password, verify_password
from ..schemas import (
    UserAuthenticateRequest,
    UserCreate,
    UserResponse,
    UserUpdate,
)

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
    # Hashing is done here (server-side) so the HTTP boundary never
    # carries bcrypt hashes — plaintext in, hashes-at-rest only.
    try:
        u = user_svc.create_user(
            db,
            email=body.email,
            password_hash=hash_password(body.password),
            role=body.role,
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
            role=body.role,
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
    # Refuse to delete the last admin — locks would strand the UI.
    u = user_svc.get_user(db, user_id)
    if u and u.role == "admin":
        remaining = [
            x for x in user_svc.list_users(db)
            if x.role == "admin" and not x.disabled and x.id != user_id
        ]
        if not remaining:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot delete the last remaining active admin",
            )
    if not user_svc.delete_user(db, user_id):
        raise HTTPException(status_code=404, detail="User not found")


@router.post("/authenticate", response_model=UserResponse)
def authenticate_user(
    body: UserAuthenticateRequest,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    """Verify (email, password). 401 on invalid or disabled.

    Centralizing bcrypt here keeps the hash out of the wire between
    mcp_server and the Web UI.
    """
    u = user_svc.get_user_by_email(db, body.email)
    if u is None or u.disabled or not verify_password(body.password, u.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )
    user_svc.touch_login(db, u.id)
    return _to_response(u)
