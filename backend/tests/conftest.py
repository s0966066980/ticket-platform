"""Public-interface test harness with disposable PostgreSQL 16 databases.

Set TEST_DATABASE_URL to a PostgreSQL16 server on which the caller may create
and drop databases. Existing databases are never truncated or dropped: a random
per-session database is created first, and only that generated name is removed.
"""
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_engine, get_session
from app.main import create_app
from app.models import Base


@pytest.fixture(scope='session')
def test_database_url():
    provided = os.environ.get('TEST_DATABASE_URL')
    if not provided:
        pytest.skip('TEST_DATABASE_URL not set: real PostgreSQL 16 integration execution unavailable')
    url = make_url(provided).set(drivername='postgresql+psycopg')
    admin = create_engine(url.set(database='postgres'), isolation_level='AUTOCOMMIT')
    name = 'ticket_test_' + uuid4().hex
    try:
        with admin.connect() as connection:
            version = int(connection.scalar(text('SHOW server_version_num')))
            if version // 10000 != 16:
                pytest.fail(f'Integration tests require PostgreSQL16, got server_version_num={version}')
            connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
        test_url = url.set(database=name).render_as_string(hide_password=False)
        previous = os.environ.get('DATABASE_URL')
        os.environ['DATABASE_URL'] = test_url
        get_settings.cache_clear()
        get_engine.cache_clear()
        config = Config(str(Path(__file__).parents[1] / 'alembic.ini'))
        command.upgrade(config, 'head')
        try:
            yield test_url
        finally:
            get_engine().dispose()
            get_engine.cache_clear()
            if previous is None:
                os.environ.pop('DATABASE_URL', None)
            else:
                os.environ['DATABASE_URL'] = previous
            get_settings.cache_clear()
    finally:
        with admin.connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.dispose()


@pytest.fixture
def db_session(test_database_url):
    engine = get_engine()
    with engine.begin() as connection:
        # Every identifier comes from known metadata, never external input.
        tables = ', '.join(connection.dialect.identifier_preparer.format_table(table) for table in Base.metadata.tables.values())
        connection.exec_driver_sql(f'TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE')
    with Session(engine, expire_on_commit=False) as session:
        yield session


@pytest.fixture
def client(db_session):
    app = create_app()

    def override_session():
        yield db_session

    app.dependency_overrides[get_session] = override_session
    with TestClient(app) as test_client:
        yield test_client
