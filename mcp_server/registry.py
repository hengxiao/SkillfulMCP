from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from .models import Agent
from .schemas import AgentCreate, AgentUpdate


def create_agent(db: Session, data: AgentCreate) -> Agent:
    agent = Agent(
        id=data.id,
        name=data.name,
        skillsets=data.skillsets,
        skills=data.skills,
        scope=data.scope,
    )
    db.add(agent)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ValueError(f"Agent {data.id!r} already exists")
    db.commit()
    db.refresh(agent)
    return agent


def get_agent(db: Session, agent_id: str) -> Agent | None:
    return db.get(Agent, agent_id)


def list_agents(db: Session) -> list[Agent]:
    return db.query(Agent).all()


def update_agent(db: Session, agent_id: str, data: AgentUpdate) -> Agent | None:
    agent = db.get(Agent, agent_id)
    if not agent:
        return None
    if data.name is not None:
        agent.name = data.name
    if data.skillsets is not None:
        agent.skillsets = data.skillsets
    if data.skills is not None:
        agent.skills = data.skills
    if data.scope is not None:
        agent.scope = data.scope
    db.commit()
    db.refresh(agent)
    return agent


def delete_agent(db: Session, agent_id: str) -> bool:
    agent = db.get(Agent, agent_id)
    if not agent:
        return False
    db.delete(agent)
    db.commit()
    return True
