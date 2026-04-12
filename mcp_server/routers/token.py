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
    token = issue_token(agent, expires_in=request.expires_in)
    return TokenResponse(access_token=token, expires_in=request.expires_in)
