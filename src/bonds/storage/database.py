"""Engine and session management."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from bonds.config import DatabaseSettings, get_settings
from bonds.storage.schema import Base


class Database:
    """Owns the SQLAlchemy engine and hands out transactional sessions."""

    def __init__(self, settings: DatabaseSettings | None = None) -> None:
        self._settings = settings or get_settings().db
        self._engine: Engine = create_engine(self._settings.url, pool_pre_ping=True, future=True)
        self._session_factory = sessionmaker(bind=self._engine, expire_on_commit=False)

    @property
    def engine(self) -> Engine:
        """The underlying SQLAlchemy engine."""
        return self._engine

    def create_all(self) -> None:
        """Create every table defined on :class:`~bonds.storage.schema.Base`.

        Convenience for local bootstrap; Alembic migrations are the source of truth.
        """
        Base.metadata.create_all(self._engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Provide a transactional session scope (commit on success, rollback on error)."""
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
