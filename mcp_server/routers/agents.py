from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..dependencies import get_db, require_admin
from ..registry import create_agent, delete_agent, get_agent, list_agents, update_agent
from ..schemas import AgentCreate, AgentResponse, AgentUpdate

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("", response_model=list[AgentResponse])
def list_agents_endpoint(
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    return list_agents(db)


@router.get("/{agent_id}", response_model=AgentResponse)
def get_agent_endpoint(
    agent_id: str,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    agent = get_agent(db, agent_id)
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    return agent


@router.post("", response_model=AgentResponse, status_code=status.HTTP_201_CREATED)
def create_agent_endpoint(
    data: AgentCreate,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    try:
        return create_agent(db, data)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.put("/{agent_id}", response_model=AgentResponse)
def update_agent_endpoint(
    agent_id: str,
    data: AgentUpdate,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    agent = update_agent(db, agent_id, data)
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    return agent


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_agent_endpoint(
    agent_id: str,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    if not delete_agent(db, agent_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
