from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..auth import issue_token
from ..dependencies import get_db, require_admin
from ..registry import get_agent
from ..schemas import TokenRequest, TokenResponse

router = APIRouter(tags=["token"])


@router.post("/token", response_model=TokenResponse)
def create_token(
    request: TokenRequest,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    """
    Issue a signed JWT for a registered agent.
    Requires X-Admin-Key. Treat this endpoint as a privileged operation.
    """
    agent = get_agent(db, request.agent_id)
    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent {request.agent_id!r} not found",
        )
    # Wave 8c: narrowing — each supplied list must be a subset of the
    # agent's own grants. Prevents an admin key from over-broadening a
    # token beyond what the registered agent allows.
    def _check_subset(name: str, got: list[str] | None, allowed: list[str]) -> None:
        if got is None:
            return
        extra = set(got) - set(allowed or [])
        if extra:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{name} contains values not granted to the agent: {sorted(extra)}",
            )
    _check_subset("skills", request.skills, agent.skills or [])
    _check_subset("skillsets", request.skillsets, agent.skillsets or [])
    _check_subset("scope", request.scope, agent.scope or [])

    token = issue_token(
        agent,
        expires_in=request.expires_in,
        skills=request.skills,
        skillsets=request.skillsets,
        scope=request.scope,
    )
    return TokenResponse(access_token=token, expires_in=request.expires_in)
