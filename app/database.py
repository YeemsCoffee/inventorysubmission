"""Database engine, session factory and Base.

Supports SQLite (local/testing) and PostgreSQL (production) from a single
DATABASE_URL. Service functions own their own transactions so that every
inventory mutation is atomic.
"""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings

settings = get_settings()

# SQLite needs check_same_thread=False for the threaded dev server / scheduler.
connect_args = {"check_same_thread": False} if settings.is_sqlite else {}

engine = create_engine(
    settings.database_url,
    connect_args=connect_args,
    pool_pre_ping=not settings.is_sqlite,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    """FastAPI dependency: yields a session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create all tables. MVP uses create_all; production should adopt Alembic."""
    from . import models  # noqa: F401  (ensure models are imported/registered)

    Base.metadata.create_all(bind=engine)
