"""Engine and session management."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from bonds.config import DatabaseSettings, get_settings
from bonds.storage.schema import Base


class Crud:
    """Raw-SQL handle for exploration and ad-hoc work: run a statement, get rows back.

    Deliberately outside the repository layer — pipelines use repositories so their writes
    stay typed and auditable. This is for notebooks, EDA and one-off inspection.

    Parameters are always bound, never interpolated. Two styles, chosen by what you pass:

        crud.execute("SELECT * FROM securities WHERE isin = %s", ["IN0020160035"])
        crud.execute("SELECT * FROM securities WHERE isin = :isin", {"isin": "IN0020160035"})

    Each call runs in its own transaction, committed on success — so writes work too, but
    a multi-statement unit of work belongs in ``Database.session()`` instead.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def execute(
        self, query: str, params: Sequence[Any] | Mapping[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Run ``query`` and return its rows as dicts (empty list if it returns none)."""
        with self._engine.begin() as conn:
            if params is None:
                result = conn.execute(text(query))
            elif isinstance(params, Mapping):
                result = conn.execute(text(query), dict(params))
            else:
                # Positional binds go through the driver's own paramstyle (%s for psycopg);
                # SQLAlchemy's text() only speaks :name.
                result = conn.exec_driver_sql(query, tuple(params))
            if not result.returns_rows:
                return []
            return [dict(row) for row in result.mappings()]

    def scalar(self, query: str, params: Sequence[Any] | Mapping[str, Any] | None = None) -> Any:
        """Run ``query`` and return the first column of the first row (``None`` if no rows)."""
        rows = self.execute(query, params)
        return next(iter(rows[0].values())) if rows else None


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

    def crud(self) -> Crud:
        """A raw-SQL handle for exploration/EDA (see :class:`Crud`)."""
        return Crud(self._engine)

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
