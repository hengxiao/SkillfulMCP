"""Admin-gated share CRUD endpoints (Wave 9.4).

Exposes:
  POST   /skills/{id}/shares              add allow-list entry
  GET    /skills/{id}/shares              list entries
  DELETE /skills/{id}/shares/{share_id}

  POST   /skillsets/{id}/shares
  GET    /skillsets/{id}/shares
  DELETE /skillsets/{id}/shares/{share_id}

All routes are admin-key gated for Wave 9.4. The Web UI composes
these behind its session-cookie auth in Wave 9.5-next (tab on the
sharing card). Session-scoped role checks (owner / account-admin /
superadmin) layer on once session state carries account + role;
today's admin-key path is the backstop.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .. import shares as share_svc
from ..dependencies import get_db, require_admin
from ..schemas import ShareCreateRequest, ShareResponse

router = APIRouter(tags=["shares"])


# ---------------------------------------------------------------------------
# Skill shares
# ---------------------------------------------------------------------------

@router.post(
    "/skills/{skill_id}/shares",
    response_model=ShareResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_skill_share(
    skill_id: str,
    body: ShareCreateRequest,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    try:
        row = share_svc.add_skill_share(
            db, skill_id=skill_id, email=body.email
        )
    except share_svc.ShareError as exc:
        msg = str(exc)
        # Duplicate uniqueness hit is the only 409 path here; every
        # other ShareError is a validation 400.
        code = (
            status.HTTP_409_CONFLICT
            if "already shared" in msg
            else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(status_code=code, detail=msg)
    return ShareResponse.model_validate(row)


@router.get("/skills/{skill_id}/shares", response_model=list[ShareResponse])
def list_skill_shares(
    skill_id: str,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    return [
        ShareResponse.model_validate(s)
        for s in share_svc.list_skill_shares(db, skill_id)
    ]


@router.delete(
    "/skills/{skill_id}/shares/{share_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_skill_share(
    skill_id: str,
    share_id: int,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    # Scope the delete to the skill path so cross-resource ids are
    # 404 rather than succeeding quietly.
    from ..models import SkillShare

    row = db.get(SkillShare, share_id)
    if row is None or row.skill_id != skill_id:
        raise HTTPException(status_code=404, detail="Share not found")
    if not share_svc.delete_skill_share(db, share_id):
        raise HTTPException(status_code=404, detail="Share not found")


# ---------------------------------------------------------------------------
# Skillset shares (parallel)
# ---------------------------------------------------------------------------

@router.post(
    "/skillsets/{skillset_id}/shares",
    response_model=ShareResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_skillset_share(
    skillset_id: str,
    body: ShareCreateRequest,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    try:
        row = share_svc.add_skillset_share(
            db, skillset_id=skillset_id, email=body.email
        )
    except share_svc.ShareError as exc:
        msg = str(exc)
        code = (
            status.HTTP_409_CONFLICT
            if "already shared" in msg
            else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(status_code=code, detail=msg)
    return ShareResponse.model_validate(row)


@router.get(
    "/skillsets/{skillset_id}/shares",
    response_model=list[ShareResponse],
)
def list_skillset_shares(
    skillset_id: str,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    return [
        ShareResponse.model_validate(s)
        for s in share_svc.list_skillset_shares(db, skillset_id)
    ]


@router.delete(
    "/skillsets/{skillset_id}/shares/{share_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_skillset_share(
    skillset_id: str,
    share_id: int,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    from ..models import SkillsetShare

    row = db.get(SkillsetShare, share_id)
    if row is None or row.skillset_id != skillset_id:
        raise HTTPException(status_code=404, detail="Share not found")
    if not share_svc.delete_skillset_share(db, share_id):
        raise HTTPException(status_code=404, detail="Share not found")
