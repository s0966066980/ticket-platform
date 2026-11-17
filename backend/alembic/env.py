from alembic import context
from sqlalchemy import create_engine, pool

from app.config import get_settings
from app.models import Base

config = context.config
url = get_settings().database_url


def run_migrations_offline():
    context.configure(url=url, target_metadata=Base.metadata, literal_binds=True, include_schemas=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    with create_engine(url, poolclass=pool.NullPool).connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata, include_schemas=True, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
