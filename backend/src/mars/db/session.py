"""Engine, session and transaction management.

Route handlers never open a transaction themselves. They receive a session from
the request dependency; the dependency commits on success and rolls back on any
exception, so a partially applied write cannot escape a failed request.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, sessionmaker

from mars.core.logging import get_logger
from mars.core.settings import Settings, get_settings

logger = get_logger(__name__)


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Return the process-wide SQLAlchemy engine."""
    settings = get_settings()
    return _build_engine(settings)


def _build_engine(settings: Settings) -> Engine:
    engine = create_engine(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_timeout=settings.database_pool_timeout_seconds,
        pool_pre_ping=True,
        echo=settings.database_echo,
        future=True,
        connect_args={
            # Bounded, so an unreachable database surfaces as a fast, legible
            # readiness failure rather than a hung probe.
            "connect_timeout": settings.database_connect_timeout_seconds,
            "application_name": "mars-api",
        },
    )

    statement_timeout = settings.database_statement_timeout_ms

    @event.listens_for(engine, "connect")
    def _set_session_defaults(dbapi_connection: object, _record: object) -> None:
        # A runaway analytical query must not hold a connection indefinitely.
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        try:
            cursor.execute(f"SET statement_timeout = {statement_timeout}")
            cursor.execute("SET TIME ZONE 'UTC'")
        finally:
            cursor.close()

    return engine


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(
        bind=get_engine(),
        class_=Session,
        expire_on_commit=False,
        autoflush=False,
        future=True,
    )


def get_db_session() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a transactional session.

    Commits when the handler returns normally, rolls back on any exception.
    """
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for workers, scripts and tests."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def check_database(connection: Connection) -> dict[str, object]:
    """Probe the database for the readiness endpoint.

    Reports the PostgreSQL server version and, when the extension is installed,
    the PostGIS version. A missing PostGIS extension is reported rather than
    treated as a failure: the schema work of phases 1-2 does not require it, and
    the geography importer (Prompt 5) checks for it explicitly.
    """
    server_version = connection.execute(text("SHOW server_version")).scalar_one()

    postgis_version: str | None = None
    postgis_available = False
    try:
        postgis_version = connection.execute(text("SELECT PostGIS_Lib_Version()")).scalar_one()
        postgis_available = True
    except Exception:
        postgis_version = None
        postgis_available = False

    return {
        "server_version": str(server_version),
        "postgis_available": postgis_available,
        "postgis_version": postgis_version,
    }


def reset_engine_cache() -> None:
    """Dispose the engine and clear the caches. Used by tests."""
    if get_engine.cache_info().currsize:
        get_engine().dispose()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
