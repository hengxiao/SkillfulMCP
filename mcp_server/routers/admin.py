"""
Admin-only read endpoints that the Web UI uses instead of the JWT-protected
agent-facing endpoints.  All routes here require X-Admin-Key.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..catalog import get_skill_versions, list_skills_in_skillset
from ..dependencies import get_db, require_admin
from ..models import Skill
from ..schemas import SkillResponse, SkillVersionInfo

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
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    """Get the latest version of a skill (admin, no JWT required)."""
    skill = (
        db.query(Skill)
        .filter(Skill.id == skill_id, Skill.is_latest.is_(True))
        .first()
    )
    if not skill:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    return _to_response(skill)


@router.get("/skillsets/{skillset_id}/skills", response_model=list[SkillResponse])
def list_skillset_skills_admin(
    skillset_id: str,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    """List all skills in a skillset (admin, no JWT required)."""
    skills = list_skills_in_skillset(db, skillset_id)
    return [_to_response(s) for s in skills]
