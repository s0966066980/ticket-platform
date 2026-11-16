from functools import lru_cache
from collections.abc import Iterator

from sqlalchemy import create_engine, select, func
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.config import get_settings


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        connect_args={"connect_timeout": 5},
    )


def get_session() -> Iterator[Session]:
    """Yield a session; mutation services must commit their own atomic operation.

    No implicit commit after sending a response. A route may share the session
    with auth reads (autobegin); transaction services flush/commit explicitly.
    An exception or read-only request rolls back pending work on close.
    """
    with Session(get_engine(), expire_on_commit=False) as session:
        yield session


def new_session() -> Session:
    return Session(get_engine(), expire_on_commit=False)


def db_now(session: Session):
    return session.scalar(select(func.clock_timestamp()))
