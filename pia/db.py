"""SQLAlchemy database engine, session factory, and declarative Base."""

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import Settings


class Base(DeclarativeBase):
    """Declarative base class for ORM models."""


def make_engine(settings: Settings):
    """Create SQLAlchemy engine from settings."""
    return create_engine(settings.database_url)


def make_session_factory(engine) -> sessionmaker[Session]:
    """Create session factory bound to engine."""
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """Yield a session, ensuring it is closed after use."""
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
