from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
import semver

from .models import Account, Skill, SkillFile, Skillset, SkillSkillset
from .schemas import SkillCreate, SkillsetCreate


# Wave 9.2 — default account id resolver for admin-key callers that
# don't pass account_id explicitly. Never raises: if no default
# account exists (fresh deployment pre-bootstrap), returns None and
# the caller's Integrity error path surfaces the issue clearly.
def _resolve_default_account_id(db: Session) -> str | None:
    row = db.query(Account).filter(Account.name == "default").first()
    return row.id if row else None


def _stamp_account(
    db: Session, requested: str | None
) -> str:
    """Return the account_id to stamp on a new catalog row.

    Preference order:
      1. explicit `requested` value (must exist).
      2. the `default` account (created by Wave 9.0 bootstrap or
         Wave 9.2 migration).

    Raises ValueError when neither is available so the handler
    returns a friendly 400/409 instead of letting an IntegrityError
    bubble up.
    """
    if requested:
        if db.get(Account, requested) is None:
            raise ValueError(f"account {requested!r} does not exist")
        return requested
    default_id = _resolve_default_account_id(db)
    if default_id is None:
        raise ValueError(
            "no default account exists; create one via POST /admin/accounts "
            "or pass account_id explicitly"
        )
    return default_id


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _refresh_is_latest(db: Session, skill_id: str) -> None:
    """Recompute is_latest for every version of skill_id using semver ordering."""
    rows: list[Skill] = db.query(Skill).filter(Skill.id == skill_id).all()
    if not rows:
        return
    latest = max(rows, key=lambda r: semver.Version.parse(r.version))
    for row in rows:
        row.is_latest = row.pk == latest.pk
    db.flush()


def _ensure_link(db: Session, skill_id: str, skillset_id: str) -> None:
    exists = (
        db.query(SkillSkillset)
        .filter(
            SkillSkillset.skill_id == skill_id,
            SkillSkillset.skillset_id == skillset_id,
        )
        .first()
    )
    if not exists:
        db.add(SkillSkillset(skill_id=skill_id, skillset_id=skillset_id))


# ---------------------------------------------------------------------------
# Skill CRUD
# ---------------------------------------------------------------------------

def create_skill(db: Session, data: SkillCreate) -> Skill:
    """Create a new skill version. Raises ValueError on duplicate id+version."""
    for ss_id in data.skillset_ids:
        if not db.get(Skillset, ss_id):
            raise ValueError(f"Skillset {ss_id!r} does not exist")

    account_id = _stamp_account(db, data.account_id)
    skill = Skill(
        id=data.id,
        name=data.name,
        description=data.description,
        version=data.version,
        metadata_=data.metadata,
        is_latest=False,
        visibility=getattr(data, "visibility", "private"),
        account_id=account_id,
        owner_user_id=data.owner_user_id,
    )
    db.add(skill)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ValueError(
            f"Skill {data.id!r} version {data.version!r} already exists"
        )

    for ss_id in data.skillset_ids:
        _ensure_link(db, data.id, ss_id)

    _refresh_is_latest(db, data.id)
    db.commit()
    db.refresh(skill)
    return skill


def upsert_skill(
    db: Session,
    skill_id: str,
    name: str,
    description: str,
    version: str,
    metadata: dict,
    visibility: str = "private",
    *,
    account_id: str | None = None,
    owner_user_id: str | None = None,
) -> Skill:
    """Replace an existing skill version or create it if absent.

    Wave 9.2: when creating a net-new row, account_id is stamped via
    _stamp_account. Updating an existing row does NOT rewrite
    account_id or ownership — those stay with the original record
    unless the caller explicitly overrides (via account_id / owner_user_id
    kwargs). Keeps the "upsert a skill version" flow from silently
    moving content between accounts.
    """
    existing = (
        db.query(Skill)
        .filter(Skill.id == skill_id, Skill.version == version)
        .first()
    )
    if existing:
        existing.name = name
        existing.description = description
        existing.metadata_ = metadata
        existing.visibility = visibility
        if account_id:
            existing.account_id = _stamp_account(db, account_id)
        if owner_user_id is not None:
            existing.owner_user_id = owner_user_id
        db.flush()
        _refresh_is_latest(db, skill_id)
        db.commit()
        db.refresh(existing)
        return existing

    stamped_account = _stamp_account(db, account_id)
    skill = Skill(
        id=skill_id,
        name=name,
        description=description,
        version=version,
        metadata_=metadata,
        is_latest=False,
        visibility=visibility,
        account_id=stamped_account,
        owner_user_id=owner_user_id,
    )
    db.add(skill)
    db.flush()
    _refresh_is_latest(db, skill_id)
    db.commit()
    db.refresh(skill)
    return skill


def get_skill_latest(db: Session, skill_id: str) -> Skill | None:
    return (
        db.query(Skill)
        .filter(Skill.id == skill_id, Skill.is_latest.is_(True))
        .first()
    )


def get_skill_version(db: Session, skill_id: str, version: str) -> Skill | None:
    return (
        db.query(Skill)
        .filter(Skill.id == skill_id, Skill.version == version)
        .first()
    )


def get_skill_versions(db: Session, skill_id: str) -> list[Skill]:
    rows = db.query(Skill).filter(Skill.id == skill_id).all()
    return sorted(rows, key=lambda r: semver.Version.parse(r.version))


def delete_skill_all(db: Session, skill_id: str) -> int:
    # Capture pks before deleting rows so we can clean up bundle files.
    pks = [pk for (pk,) in db.query(Skill.pk).filter(Skill.id == skill_id).all()]
    if pks:
        db.query(SkillFile).filter(SkillFile.skill_pk.in_(pks)).delete(
            synchronize_session=False
        )
    n = db.query(Skill).filter(Skill.id == skill_id).delete()
    # Also remove orphaned SkillSkillset rows (no CASCADE from Skill since no FK)
    db.query(SkillSkillset).filter(SkillSkillset.skill_id == skill_id).delete()
    db.commit()
    return n


def delete_skill_version(db: Session, skill_id: str, version: str) -> bool:
    target = (
        db.query(Skill)
        .filter(Skill.id == skill_id, Skill.version == version)
        .first()
    )
    if target is None:
        return False
    db.query(SkillFile).filter(SkillFile.skill_pk == target.pk).delete(
        synchronize_session=False
    )
    n = db.query(Skill).filter(Skill.pk == target.pk).delete()
    if n == 0:
        return False
    # Remove SkillSkillset links if no versions remain
    remaining = db.query(Skill).filter(Skill.id == skill_id).count()
    if remaining == 0:
        db.query(SkillSkillset).filter(SkillSkillset.skill_id == skill_id).delete()
    else:
        _refresh_is_latest(db, skill_id)
    db.commit()
    return True


def list_skills_for_agent(
    db: Session,
    allowed_ids: set[str],
    *,
    limit: int | None = None,
) -> list[Skill]:
    """Return the latest version of each skill the agent is allowed to access.

    If `limit` is given, cap the result at that many rows. No cursor /
    keyset pagination yet — that's a follow-up wave. Rows are ordered by
    `id` so a capped response is deterministic.
    """
    if not allowed_ids:
        return []
    q = (
        db.query(Skill)
        .filter(Skill.id.in_(allowed_ids), Skill.is_latest.is_(True))
        .order_by(Skill.id)
    )
    if limit is not None:
        q = q.limit(limit)
    return q.all()


# ---------------------------------------------------------------------------
# Skillset CRUD
# ---------------------------------------------------------------------------

def create_skillset(db: Session, data: SkillsetCreate) -> Skillset:
    account_id = _stamp_account(db, data.account_id)
    ss = Skillset(
        id=data.id,
        name=data.name,
        description=data.description,
        visibility=getattr(data, "visibility", "private"),
        account_id=account_id,
        owner_user_id=data.owner_user_id,
    )
    db.add(ss)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ValueError(f"Skillset {data.id!r} already exists")
    db.commit()
    db.refresh(ss)
    return ss


def upsert_skillset(db: Session, skillset_id: str, data: SkillsetCreate) -> Skillset:
    existing = db.get(Skillset, skillset_id)
    visibility = getattr(data, "visibility", "private")
    if existing:
        existing.name = data.name
        existing.description = data.description
        existing.visibility = visibility
        if data.account_id:
            existing.account_id = _stamp_account(db, data.account_id)
        if data.owner_user_id is not None:
            existing.owner_user_id = data.owner_user_id
        db.commit()
        db.refresh(existing)
        return existing
    account_id = _stamp_account(db, data.account_id)
    ss = Skillset(
        id=skillset_id,
        name=data.name,
        description=data.description,
        visibility=visibility,
        account_id=account_id,
        owner_user_id=data.owner_user_id,
    )
    db.add(ss)
    db.commit()
    db.refresh(ss)
    return ss


def get_skillset(db: Session, skillset_id: str) -> Skillset | None:
    return db.get(Skillset, skillset_id)


def list_skillsets(db: Session) -> list[Skillset]:
    return db.query(Skillset).all()


def delete_skillset(db: Session, skillset_id: str) -> bool:
    ss = db.get(Skillset, skillset_id)
    if not ss:
        return False
    db.delete(ss)
    db.commit()
    return True


def list_skills_in_skillset(db: Session, skillset_id: str) -> list[Skill]:
    """Return latest version of each skill in the skillset."""
    rows = (
        db.query(SkillSkillset.skill_id)
        .filter(SkillSkillset.skillset_id == skillset_id)
        .all()
    )
    skill_ids = [r.skill_id for r in rows]
    if not skill_ids:
        return []
    return (
        db.query(Skill)
        .filter(Skill.id.in_(skill_ids), Skill.is_latest.is_(True))
        .all()
    )


def add_skill_to_skillset(db: Session, skillset_id: str, skill_id: str) -> None:
    if not db.get(Skillset, skillset_id):
        raise ValueError(f"Skillset {skillset_id!r} does not exist")
    if not db.query(Skill).filter(Skill.id == skill_id).first():
        raise ValueError(f"Skill {skill_id!r} does not exist")
    _ensure_link(db, skill_id, skillset_id)
    db.commit()


def remove_skill_from_skillset(db: Session, skillset_id: str, skill_id: str) -> bool:
    n = (
        db.query(SkillSkillset)
        .filter(
            SkillSkillset.skillset_id == skillset_id,
            SkillSkillset.skill_id == skill_id,
        )
        .delete()
    )
    db.commit()
    return n > 0
