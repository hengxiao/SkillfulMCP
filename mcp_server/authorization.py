from __future__ import annotations

from typing import Any, Mapping

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


# ---------------------------------------------------------------------------
# Wave 9.2 — operator UI visibility check
# ---------------------------------------------------------------------------
#
# `resolve_allowed_skill_ids` above governs agent JWT scope. The helper
# below governs the *operator* (human via the Web UI) view: can this
# logged-in user read this catalog row?
#
# The rule follows spec §4.4:
#
#   def can_read(resource, user) -> bool:
#     if resource.visibility == "public":           return True
#     if user is None:                              return False
#     if user.is_superadmin:                        return True
#     in_account = resource.account_id in user_memberships
#     role_in_account = user_memberships.get(resource.account_id)
#     if resource.owner_user_id == user.user_id:    return True
#     if resource.visibility == "account":
#         return in_account or share_exists(resource, user.email)
#     # visibility == "private"
#     if not in_account:                            return False
#     if role_in_account == "account-admin":        return True
#     return share_exists(resource, user.email)
#
# Shares (skill_shares / skillset_shares) don't exist yet — Wave 9.4
# adds them; until then `share_exists` is always False.
#
# Wave 9.2 keeps this helper pure-Python + side-effect-free. The UI
# in Wave 9.5 will wire it into the /skills GET filter and the
# per-resource PUT guards.


def can_read(
    resource: Any,
    *,
    is_superadmin: bool = False,
    user_id: str | None = None,
    user_email: str | None = None,
    user_memberships: Mapping[str, str] | None = None,
    share_exists_fn=None,
) -> bool:
    """Operator read check for a catalog row.

    Parameters
    ----------
    resource
        The ORM row. Reads `.visibility`, `.account_id`, `.owner_user_id`
        attributes — works with :class:`Skill`, :class:`Skillset`,
        :class:`Agent`, or any object with that shape.
    is_superadmin
        Skip every other check when True.
    user_id, user_email
        Session user attributes. `None` means anonymous.
    user_memberships
        `{account_id: role}` dict. `{}` means "logged in but in no
        accounts"; `None` is treated the same way.
    share_exists_fn
        Optional callable `(resource, email) -> bool` used to
        consult the Wave 9.4 allow-list tables. Default returns False
        so the helper stays callable before 9.4 lands.
    """
    visibility = getattr(resource, "visibility", "private")
    if visibility == "public":
        return True
    if is_superadmin:
        return True
    if user_id is None:
        return False

    if getattr(resource, "owner_user_id", None) == user_id:
        return True

    mems = user_memberships or {}
    account_id = getattr(resource, "account_id", None)
    in_account = account_id is not None and account_id in mems
    role_in_account = mems.get(account_id) if account_id is not None else None

    share_exists = False
    if share_exists_fn is not None and user_email:
        try:
            share_exists = bool(share_exists_fn(resource, user_email))
        except Exception:
            # The caller owns share-table access; defensive swallow so
            # a transient SQL error doesn't deny a legitimate read
            # through the owner / membership paths above.
            share_exists = False

    if visibility == "account":
        return in_account or share_exists

    # visibility == "private"
    if not in_account:
        return False
    if role_in_account == "account-admin":
        return True
    return share_exists
