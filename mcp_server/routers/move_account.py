"""Cross-account catalog migration (spec §10 → item L).

Shipping endpoints:
  POST /admin/skills/{id}/move-account     body: {target_account_id}
  POST /admin/skillsets/{id}/move-account
  POST /admin/agents/{id}/move-account

All admin-key gated. Atomically updates `account_id` on the row (and
every version for a skill id, since versions share the logical id).
Clears the `owner_user_id` + allow-list entries because they belong
to the old account's sharing context — keeping them would silently
grant the old owner read access in the new tenant.

Scope decision:
- Only catalog rows move. Associated bundle files + skillset links
  follow because they're FK-scoped to the row.
- Agents can be moved individually; their grants (skill / skillset
  claims) are NOT rewritten. If an agent moves to an account where
  its granted resources don't exist, its minted tokens will produce
  403s at agent runtime — the superadmin doing the move is
  responsible for double-checking. Documented here rather than
  silently filtered.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import accounts as acct_svc
from ..dependencies import get_db, require_admin
from ..logging_config import get_logger
from ..models import Agent, Skill, SkillShare, Skillset, SkillsetShare

_log = get_logger("mcp.move_account")

router = APIRouter(prefix="/admin", tags=["admin", "accounts"])


class _MoveAccountRequest(BaseModel):
    target_account_id: str


def _validate_target(db: Session, target_id: str) -> None:
    if acct_svc.get_account(db, target_id) is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"target account {target_id!r} does not exist",
        )


@router.post("/skills/{skill_id}/move-account", status_code=200)
def move_skill(
    skill_id: str,
    body: _MoveAccountRequest,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    """Move every version of `skill_id` into `target_account_id`.

    Clears `owner_user_id` + `owner_email_snapshot` (the previous
    owner's memberships are in the old tenant) and deletes every
    `skill_shares` row for the id (shares belong to the old
    account's sharing context).
    """
    _validate_target(db, body.target_account_id)

    versions = db.query(Skill).filter(Skill.id == skill_id).all()
    if not versions:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found"
        )
    prev_account_ids = {v.account_id for v in versions}
    for v in versions:
        v.account_id = body.target_account_id
        v.owner_user_id = None
        v.owner_email_snapshot = None

    # Wipe stale shares — they were granted by the old account's
    # owners.
    shares_deleted = (
        db.query(SkillShare)
        .filter(SkillShare.skill_id == skill_id)
        .delete(synchronize_session=False)
    )
    db.commit()

    _log.info(
        "catalog.moved",
        extra={
            "resource_kind": "skill",
            "resource_id": skill_id,
            "from_account_ids": sorted(prev_account_ids),
            "to_account_id": body.target_account_id,
            "shares_wiped": shares_deleted,
            "versions_moved": len(versions),
        },
    )
    return {
        "resource_id": skill_id,
        "target_account_id": body.target_account_id,
        "versions_moved": len(versions),
        "shares_wiped": shares_deleted,
    }


@router.post("/skillsets/{skillset_id}/move-account", status_code=200)
def move_skillset(
    skillset_id: str,
    body: _MoveAccountRequest,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    """Move a skillset into `target_account_id`. Does NOT move the
    skills associated through `skill_skillsets` — those can stay in
    their own accounts (a skillset is a curation overlay, not an
    ownership anchor). Allow-list entries for the skillset are
    wiped for the same reason skill shares are (§move_skill)."""
    _validate_target(db, body.target_account_id)

    ss = db.get(Skillset, skillset_id)
    if ss is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Skillset not found"
        )
    prev = ss.account_id
    ss.account_id = body.target_account_id
    ss.owner_user_id = None
    ss.owner_email_snapshot = None
    shares_deleted = (
        db.query(SkillsetShare)
        .filter(SkillsetShare.skillset_id == skillset_id)
        .delete(synchronize_session=False)
    )
    db.commit()

    _log.info(
        "catalog.moved",
        extra={
            "resource_kind": "skillset",
            "resource_id": skillset_id,
            "from_account_id": prev,
            "to_account_id": body.target_account_id,
            "shares_wiped": shares_deleted,
        },
    )
    return {
        "resource_id": skillset_id,
        "target_account_id": body.target_account_id,
        "shares_wiped": shares_deleted,
    }


@router.post("/agents/{agent_id}/move-account", status_code=200)
def move_agent(
    agent_id: str,
    body: _MoveAccountRequest,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    """Move an agent into `target_account_id`. Grants (skills,
    skillsets, scope) are NOT rewritten — if the agent's grants
    reference resources that don't exist in the new account, its
    tokens will 403 at runtime. Operators should dry-run through
    `POST /token` with a minimal `expires_in` after the move to
    verify."""
    _validate_target(db, body.target_account_id)

    agent = db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found"
        )
    prev = agent.account_id
    agent.account_id = body.target_account_id
    agent.owner_user_id = None
    agent.owner_email_snapshot = None
    db.commit()

    _log.info(
        "catalog.moved",
        extra={
            "resource_kind": "agent",
            "resource_id": agent_id,
            "from_account_id": prev,
            "to_account_id": body.target_account_id,
            "shares_wiped": 0,
        },
    )
    return {
        "resource_id": agent_id,
        "target_account_id": body.target_account_id,
    }
