"""Email-based allow-list service (Wave 9.4).

Shares are keyed by raw email — no FK to `users.email` so invitations
for unregistered addresses persist. When the allow-listed user later
signs up, the `can_read` helper in `authorization.py` matches the
share by normalized email at read time; no on-signup reconciliation
needed.

Visibility interaction (spec §4.3):
- ``public`` resources: shares are meaningless (everyone already
  sees them). The router rejects ``POST /shares`` on public rows.
- ``account`` resources: shares grant cross-account UI read access.
- ``private`` resources: a share entry is only effective when the
  grantee ALSO has a membership in the resource's account. This
  prevents a private resource from leaking to outsiders through
  the allow list — the intersection rule lives in
  ``authorization.can_read``; this module is pure data.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .logging_config import get_logger
from .models import Skill, SkillShare, Skillset, SkillsetShare
from .users import normalize_email

_log = get_logger("mcp.shares")


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ShareError(Exception):
    """Service-layer error bucket surfaced as 400/409 at the HTTP layer."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_email(email: str) -> str:
    normalized = normalize_email(email)
    if not normalized:
        raise ShareError("email is required")
    if not _EMAIL_RE.match(normalized):
        raise ShareError(f"email {email!r} is not a valid address")
    return normalized


def _assert_not_public(resource) -> None:
    if getattr(resource, "visibility", "") == "public":
        raise ShareError(
            "resource is already world-readable (visibility=public); "
            "shares are redundant"
        )


# ---------------------------------------------------------------------------
# Skill shares
# ---------------------------------------------------------------------------

def add_skill_share(
    db: Session,
    *,
    skill_id: str,
    email: str,
    granted_by_user_id: str | None = None,
) -> SkillShare:
    # Resolve the skill's visibility from the latest version — the
    # share is logical-id-keyed and applies to every version.
    skill = (
        db.query(Skill)
        .filter(Skill.id == skill_id, Skill.is_latest.is_(True))
        .first()
    )
    if skill is None:
        raise ShareError(f"skill {skill_id!r} does not exist")
    _assert_not_public(skill)

    normalized = _validate_email(email)
    row = SkillShare(
        skill_id=skill_id,
        email=normalized,
        granted_by_user_id=granted_by_user_id,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ShareError(
            f"skill {skill_id!r} already shared with {normalized!r}"
        )
    db.refresh(row)
    return row


def list_skill_shares(db: Session, skill_id: str) -> list[SkillShare]:
    return (
        db.query(SkillShare)
        .filter(SkillShare.skill_id == skill_id)
        .order_by(SkillShare.created_at)
        .all()
    )


def delete_skill_share(db: Session, share_id: int) -> bool:
    row = db.get(SkillShare, share_id)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


def skill_share_exists(db: Session, resource, email: str | None) -> bool:
    """Predicate callable for `authorization.can_read(share_exists_fn=...)`.

    Accepts the resource (Skill row) + the candidate email; returns
    True iff an entry matches on (skill_id, normalized email).
    """
    if not email:
        return False
    normalized = normalize_email(email)
    return (
        db.query(SkillShare)
        .filter(
            SkillShare.skill_id == getattr(resource, "id", None),
            SkillShare.email == normalized,
        )
        .first()
    ) is not None


# ---------------------------------------------------------------------------
# Skillset shares (parallel)
# ---------------------------------------------------------------------------

def add_skillset_share(
    db: Session,
    *,
    skillset_id: str,
    email: str,
    granted_by_user_id: str | None = None,
) -> SkillsetShare:
    skillset = db.get(Skillset, skillset_id)
    if skillset is None:
        raise ShareError(f"skillset {skillset_id!r} does not exist")
    _assert_not_public(skillset)

    normalized = _validate_email(email)
    row = SkillsetShare(
        skillset_id=skillset_id,
        email=normalized,
        granted_by_user_id=granted_by_user_id,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ShareError(
            f"skillset {skillset_id!r} already shared with {normalized!r}"
        )
    db.refresh(row)
    return row


def list_skillset_shares(db: Session, skillset_id: str) -> list[SkillsetShare]:
    return (
        db.query(SkillsetShare)
        .filter(SkillsetShare.skillset_id == skillset_id)
        .order_by(SkillsetShare.created_at)
        .all()
    )


def delete_skillset_share(db: Session, share_id: int) -> bool:
    row = db.get(SkillsetShare, share_id)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


def skillset_share_exists(db: Session, resource, email: str | None) -> bool:
    if not email:
        return False
    normalized = normalize_email(email)
    return (
        db.query(SkillsetShare)
        .filter(
            SkillsetShare.skillset_id == getattr(resource, "id", None),
            SkillsetShare.email == normalized,
        )
        .first()
    ) is not None
