"""
Admin-only read endpoints that the Web UI uses instead of the JWT-protected
agent-facing endpoints.  All routes here require X-Admin-Key.
"""

import mimetypes

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth import get_default_service
from ..bundles import get_file, list_bundle
from ..catalog import get_skill_version, get_skill_versions, list_skills_in_skillset
from ..dependencies import get_db, require_admin
from ..logging_config import get_logger
from ..models import Skill
from ..schemas import BundleFileInfoResponse, SkillResponse, SkillVersionInfo

_log = get_logger("mcp.admin")

router = APIRouter(prefix="/admin", tags=["admin"])


def _to_response(skill: Skill) -> SkillResponse:
    return SkillResponse(
        id=skill.id,
        name=skill.name,
        description=skill.description,
        version=skill.version,
        is_latest=skill.is_latest,
        metadata=skill.metadata_ or {},
        created_at=skill.created_at,
        updated_at=skill.updated_at,
    )


@router.get("/skills", response_model=list[SkillResponse])
def list_all_skills(
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    """List the latest version of every skill in the catalog."""
    skills = (
        db.query(Skill)
        .filter(Skill.is_latest.is_(True))
        .order_by(Skill.id)
        .all()
    )
    return [_to_response(s) for s in skills]


@router.get("/skills/{skill_id}/versions", response_model=list[SkillVersionInfo])
def list_skill_versions(
    skill_id: str,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    """List all versions of a skill, sorted by semver ascending."""
    versions = get_skill_versions(db, skill_id)
    if not versions:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    return [
        SkillVersionInfo(version=v.version, is_latest=v.is_latest, created_at=v.created_at)
        for v in versions
    ]


@router.get("/skills/{skill_id}", response_model=SkillResponse)
def get_skill_admin(
    skill_id: str,
    version: str | None = Query(default=None, description="Specific version; omit for latest"),
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    """Get a skill (latest or a specific version) — admin, no JWT required."""
    if version:
        skill = get_skill_version(db, skill_id, version)
    else:
        skill = (
            db.query(Skill)
            .filter(Skill.id == skill_id, Skill.is_latest.is_(True))
            .first()
        )
    if not skill:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    return _to_response(skill)


@router.get(
    "/skills/{skill_id}/versions/{version}/files",
    response_model=list[BundleFileInfoResponse],
)
def list_bundle_files_admin(
    skill_id: str,
    version: str,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    """List files in a skill version's bundle (admin, no JWT)."""
    skill = get_skill_version(db, skill_id, version)
    if not skill:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Skill {skill_id!r} version {version!r} not found",
        )
    return [
        BundleFileInfoResponse(path=f.path, size=f.size, sha256=f.sha256)
        for f in list_bundle(db, skill.pk)
    ]


@router.get("/skills/{skill_id}/versions/{version}/files/{path:path}")
def get_bundle_file_admin(
    skill_id: str,
    version: str,
    path: str,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    """Fetch a single bundle file (admin, no JWT)."""
    skill = get_skill_version(db, skill_id, version)
    if not skill:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Skill {skill_id!r} version {version!r} not found",
        )
    row = get_file(db, skill.pk, path)
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"File {path!r} not found"
        )
    media_type, _enc = mimetypes.guess_type(path)
    return Response(
        content=row.content,
        media_type=media_type or "application/octet-stream",
        headers={"X-Content-SHA256": row.sha256},
    )


@router.get("/skillsets/{skillset_id}/skills", response_model=list[SkillResponse])
def list_skillset_skills_admin(
    skillset_id: str,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    """List all skills in a skillset (admin, no JWT required)."""
    skills = list_skills_in_skillset(db, skillset_id)
    return [_to_response(s) for s in skills]


# ---------------------------------------------------------------------------
# Token revocation
# ---------------------------------------------------------------------------

class _RevokeRequest(BaseModel):
    jti: str


@router.post("/tokens/revoke", status_code=status.HTTP_204_NO_CONTENT)
def revoke_token(
    body: _RevokeRequest,
    _: None = Depends(require_admin),
):
    """Add a `jti` to the in-process revocation list.

    No-op if the jti is unknown (idempotent; also avoids an information-
    disclosure oracle about which jtis exist). Entries auto-expire 24h
    after insertion.
    """
    if not body.jti:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="jti is required",
        )
    service = get_default_service()
    service.revocation.revoke(body.jti)
    _log.info("token revoked", extra={"jti": body.jti})


@router.get("/tokens/revoked-count")
def revoked_count(_: None = Depends(require_admin)):
    """How many jtis are currently on the revocation list. Useful for dashboards."""
    return {"count": len(get_default_service().revocation)}
