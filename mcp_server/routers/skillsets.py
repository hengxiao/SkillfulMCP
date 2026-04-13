from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..authorization import resolve_allowed_skill_ids
from ..catalog import (
    add_skill_to_skillset,
    create_skillset,
    delete_skillset,
    get_skillset,
    list_skillsets,
    list_skills_in_skillset,
    remove_skill_from_skillset,
    upsert_skillset,
)
from ..dependencies import get_current_claims, get_db, require_admin
from ..models import Skill
from ..schemas import SkillResponse, SkillsetCreate, SkillsetResponse

router = APIRouter(prefix="/skillsets", tags=["skillsets"])


def _skill_to_response(skill: Skill) -> SkillResponse:
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
        created_at=skill.created_at,
        updated_at=skill.updated_at,
    )


@router.get("", response_model=list[SkillsetResponse])
def list_skillsets_endpoint(
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    return list_skillsets(db)


@router.get("/{skillset_id}", response_model=SkillsetResponse)
def get_skillset_endpoint(
    skillset_id: str,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    ss = get_skillset(db, skillset_id)
    if not ss:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skillset not found")
    return ss


@router.get("/{skillset_id}/skills", response_model=list[SkillResponse])
def list_skillset_skills(
    skillset_id: str,
    claims: dict = Depends(get_current_claims),
    db: Session = Depends(get_db),
):
    """
    List skills in a skillset that the requesting agent is authorized to see.
    Returns only the intersection of the skillset's skills and the agent's allowed skills.
    """
    allowed_ids = resolve_allowed_skill_ids(claims, db)
    skills = list_skills_in_skillset(db, skillset_id)
    return [_skill_to_response(s) for s in skills if s.id in allowed_ids]


@router.post("", response_model=SkillsetResponse, status_code=status.HTTP_201_CREATED)
def create_skillset_endpoint(
    data: SkillsetCreate,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    try:
        return create_skillset(db, data)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.put("/{skillset_id}", response_model=SkillsetResponse)
def upsert_skillset_endpoint(
    skillset_id: str,
    data: SkillsetCreate,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    return upsert_skillset(db, skillset_id, data)


@router.delete("/{skillset_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_skillset_endpoint(
    skillset_id: str,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    if not delete_skillset(db, skillset_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skillset not found")


@router.put("/{skillset_id}/skills/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
def associate_skill(
    skillset_id: str,
    skill_id: str,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    try:
        add_skill_to_skillset(db, skillset_id, skill_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.delete("/{skillset_id}/skills/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
def disassociate_skill(
    skillset_id: str,
    skill_id: str,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    if not remove_skill_from_skillset(db, skillset_id, skill_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Association not found",
        )
