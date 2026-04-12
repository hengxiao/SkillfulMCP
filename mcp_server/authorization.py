from sqlalchemy.orm import Session

from .models import SkillSkillset


def resolve_allowed_skill_ids(claims: dict, db: Session) -> set[str]:
    """
    Return the set of skill ids the token holder is permitted to access.

    Rule: explicit skill grants and skillset-level grants are additive (union).
    There is no deny mechanism — access is granted if either path allows it.
    """
    allowed: set[str] = set(claims.get("skills", []))

    skillset_ids: list[str] = claims.get("skillsets", [])
    if skillset_ids:
        rows = (
            db.query(SkillSkillset.skill_id)
            .filter(SkillSkillset.skillset_id.in_(skillset_ids))
            .all()
        )
        for (skill_id,) in rows:
            allowed.add(skill_id)

    return allowed
