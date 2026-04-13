from sqlalchemy.orm import Session

from .models import Skill, SkillSkillset, Skillset


def resolve_allowed_skill_ids(claims: dict, db: Session) -> set[str]:
    """
    Return the set of skill ids the token holder is permitted to access.

    Additive union of:
      1. Skills from `claims.skills` (explicit grants).
      2. Skill ids in any skillset listed in `claims.skillsets`.
      3. (Wave 8a) Every skill with `visibility = 'public'`.
      4. (Wave 8a) Every skill in any skillset with `visibility = 'public'`
         — a public skillset exposes ALL its members regardless of each
         skill's own visibility flag.

    No deny path; access is granted if any rule allows it. Public still
    requires a valid JWT — anonymous reads are a separate future flag.
    """
    allowed: set[str] = set(claims.get("skills", []))

    # 2. Granted skillsets → their member skill ids.
    skillset_ids: list[str] = claims.get("skillsets", [])
    if skillset_ids:
        rows = (
            db.query(SkillSkillset.skill_id)
            .filter(SkillSkillset.skillset_id.in_(skillset_ids))
            .all()
        )
        for (skill_id,) in rows:
            allowed.add(skill_id)

    # 3. Public skills.
    for (skill_id,) in db.query(Skill.id).filter(Skill.visibility == "public").distinct():
        allowed.add(skill_id)

    # 4. Public skillsets → ALL their member skill ids.
    public_ss = [
        sid for (sid,) in db.query(Skillset.id).filter(Skillset.visibility == "public")
    ]
    if public_ss:
        for (skill_id,) in (
            db.query(SkillSkillset.skill_id)
            .filter(SkillSkillset.skillset_id.in_(public_ss))
            .distinct()
        ):
            allowed.add(skill_id)

    return allowed
