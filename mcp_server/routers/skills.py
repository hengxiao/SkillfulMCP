from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ..authorization import resolve_allowed_skill_ids
from ..catalog import (
    create_skill,
    delete_skill_all,
    delete_skill_version,
    get_skill_latest,
    get_skill_version,
    get_skill_versions,
    list_skills_for_agent,
    upsert_skill,
)
from ..dependencies import get_current_claims, get_db, require_admin
from ..models import Skill
from ..schemas import SkillCreate, SkillResponse, SkillUpsertBody, SkillVersionInfo

router = APIRouter(prefix="/skills", tags=["skills"])


def _to_response(skill: Skill) -> SkillResponse:
    return SkillResponse(
        id=skill.id,
        name=skill.name,
        description=skill.description,
        version=skill.version,
        is_latest=skill.is_latest,
        metadata=skill.metadata_ or {},
        visibility=skill.visibility,
        created_at=skill.created_at,
        updated_at=skill.updated_at,
    )


@router.get("", response_model=list[SkillResponse])
def list_skills(
    claims: dict = Depends(get_current_claims),
    db: Session = Depends(get_db),
    limit: int | None = Query(
        default=None,
        ge=1,
        le=10_000,
        description="Optional cap on rows returned (no cursor; see productization.md for plan).",
    ),
):
    """List skills the requesting agent is authorized to access (latest version of each).

    Response shape is a flat list for backwards compatibility. Cursor-based
    pagination with a response envelope is tracked in the productization
    plan and will ship on a `/v1/` prefix once the catalog scale warrants
    it.
    """
    allowed_ids = resolve_allowed_skill_ids(claims, db)
    return [_to_response(s) for s in list_skills_for_agent(db, allowed_ids, limit=limit)]


@router.get("/{skill_id}/versions", response_model=list[SkillVersionInfo])
def list_versions(
    skill_id: str,
    claims: dict = Depends(get_current_claims),
    db: Session = Depends(get_db),
):
    """List all versions of a skill. Requires the agent to be authorized for the skill."""
    allowed_ids = resolve_allowed_skill_ids(claims, db)
    if skill_id not in allowed_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    versions = get_skill_versions(db, skill_id)
    if not versions:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    return [
        SkillVersionInfo(version=v.version, is_latest=v.is_latest, created_at=v.created_at)
        for v in versions
    ]


@router.get("/{skill_id}", response_model=SkillResponse)
def get_skill(
    skill_id: str,
    version: str | None = Query(default=None, description="Specific semver version; omit for latest"),
    claims: dict = Depends(get_current_claims),
    db: Session = Depends(get_db),
):
    """Get a skill's metadata. Returns the latest version unless ?version= is supplied."""
    allowed_ids = resolve_allowed_skill_ids(claims, db)
    if skill_id not in allowed_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    skill = get_skill_version(db, skill_id, version) if version else get_skill_latest(db, skill_id)
    if not skill:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    return _to_response(skill)


@router.post("", response_model=SkillResponse, status_code=status.HTTP_201_CREATED)
def create_skill_endpoint(
    data: SkillCreate,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    """Create a new skill version. Rejects duplicate id+version combinations."""
    try:
        skill = create_skill(db, data)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return _to_response(skill)


@router.put("/{skill_id}", response_model=SkillResponse)
def upsert_skill_endpoint(
    skill_id: str,
    data: SkillUpsertBody,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    """Upsert a skill version. Creates the record if it does not exist; replaces it if it does."""
    skill = upsert_skill(
        db,
        skill_id=skill_id,
        name=data.name,
        description=data.description,
        version=data.version,
        metadata=data.metadata,
        visibility=data.visibility,
    )
    return _to_response(skill)


@router.delete("/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_skill(
    skill_id: str,
    version: str | None = Query(default=None, description="Delete only this version; omit to delete all"),
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    """Delete a skill. Deletes all versions by default; use ?version= to target one."""
    if version:
        found = delete_skill_version(db, skill_id, version)
    else:
        found = delete_skill_all(db, skill_id) > 0
    if not found:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
