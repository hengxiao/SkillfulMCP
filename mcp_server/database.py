from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import StaticPool

from .models import Base


def make_engine(url: str):
    if url == "sqlite:///:memory:":
        # StaticPool keeps a single in-memory DB across all connections in tests
        return create_engine(
            url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    kwargs: dict = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(url, **kwargs)


def make_session_factory(engine):
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db(url: str):
    """Create engine, run DDL, return session factory."""
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    return make_session_factory(engine)
