"""
Admin-only read endpoints that the Web UI uses instead of the JWT-protected
agent-facing endpoints.  All routes here require X-Admin-Key.
"""

import mimetypes

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
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


def _to_response(skill: Skill, *, verified: bool | None = None) -> SkillResponse:
    return SkillResponse(
        id=skill.id,
        name=skill.name,
        description=skill.description,
        version=skill.version,
        is_latest=skill.is_latest,
        metadata=skill.metadata_ or {},
        visibility=skill.visibility,
        account_id=skill.account_id,
        owner_user_id=skill.owner_user_id,
        owner_email_snapshot=skill.owner_email_snapshot,
        bundle_signature_kid=skill.bundle_signature_kid,
        verified=verified,
        created_at=skill.created_at,
        updated_at=skill.updated_at,
    )


@router.get("/skills", response_model=list[SkillResponse])
def list_all_skills(
    account_id: str | None = Query(
        default=None,
        description="Filter to skills owned by a specific account.",
    ),
    mine: int = Query(
        default=0,
        ge=0, le=1,
        description=(
            "Set to 1 to limit to skills whose owner_user_id matches "
            "the header X-Owner-User-Id. Requires the admin caller to "
            "forward the logged-in user's id (the Web UI does this "
            "from the session)."
        ),
    ),
    shared: int = Query(
        default=0,
        ge=0, le=1,
        description=(
            "Set to 1 to limit to skills that the caller can see only "
            "via the allow-list (email on skill_shares). Requires "
            "header X-Owner-User-Email."
        ),
    ),
    x_owner_user_id: str | None = Header(default=None),
    x_owner_user_email: str | None = Header(default=None),
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    """List the latest version of every skill in the catalog.

    Wave 9.3: optional filters for operator UI scoping. All filters
    AND together; an unfiltered call returns every row (today's
    behavior). The "mine" + "shared" flags read the caller's id /
    email from headers the Web UI fills in from the session — the
    admin-key path doesn't have its own identity, so the Web UI
    layer is responsible for populating them.
    """
    from ..models import SkillShare

    q = db.query(Skill).filter(Skill.is_latest.is_(True))

    if account_id:
        q = q.filter(Skill.account_id == account_id)

    if mine == 1:
        if not x_owner_user_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="?mine=1 requires X-Owner-User-Id header",
            )
        q = q.filter(Skill.owner_user_id == x_owner_user_id)

    if shared == 1:
        if not x_owner_user_email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="?shared=1 requires X-Owner-User-Email header",
            )
        normalized_email = x_owner_user_email.strip().lower()
        shared_skill_ids = [
            sid for (sid,) in db.query(SkillShare.skill_id)
            .filter(SkillShare.email == normalized_email).distinct()
        ]
        # Filter to rows whose logical id appears in the share list.
        # If nothing is shared, return an empty set fast.
        if not shared_skill_ids:
            return []
        q = q.filter(Skill.id.in_(shared_skill_ids))

    skills = q.order_by(Skill.id).all()
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
    from .. import bundle_signing

    verified = bundle_signing.verify_skill(db, skill)
    return _to_response(skill, verified=verified)


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
