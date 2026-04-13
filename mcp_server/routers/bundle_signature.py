"""Skill bundle signature upload/clear (item J).

  POST   /skills/{id}/versions/{version}/signature
         body: {signature: <base64url>, kid: <key id>}

  DELETE /skills/{id}/versions/{version}/signature

Both admin-key gated. Signatures are stored but NOT validated on
POST — verification happens at read time so a rotated trust store
takes effect without re-stamping every row. This keeps sign-on-
upload a lightweight admin action.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..catalog import get_skill_version
from ..dependencies import get_db, require_admin

router = APIRouter(tags=["skills", "signing"])


class _SignatureBody(BaseModel):
    signature: str
    kid: str


@router.post(
    "/skills/{skill_id}/versions/{version}/signature",
    status_code=200,
)
def attach_signature(
    skill_id: str,
    version: str,
    body: _SignatureBody,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    skill = get_skill_version(db, skill_id, version)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill version not found")
    if not body.signature.strip() or not body.kid.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="signature + kid are required",
        )
    skill.bundle_signature = body.signature.strip()
    skill.bundle_signature_kid = body.kid.strip()
    db.commit()
    return {
        "skill_id": skill_id,
        "version": version,
        "kid": body.kid.strip(),
    }


@router.delete(
    "/skills/{skill_id}/versions/{version}/signature",
    status_code=status.HTTP_204_NO_CONTENT,
)
def clear_signature(
    skill_id: str,
    version: str,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    skill = get_skill_version(db, skill_id, version)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill version not found")
    skill.bundle_signature = None
    skill.bundle_signature_kid = None
    db.commit()
