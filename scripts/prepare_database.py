"""Serialize startup migrations and seed across API replicas."""
import argparse
import os
import subprocess
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="Run migration and seed under a PostgreSQL startup advisory lock.")
    parser.parse_args()
    from sqlalchemy import create_engine, text

    engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            # A session lock spans both child commands. A killed process drops the
            # connection, releasing it for the next replica's retry.
            connection.execute(text("SELECT pg_advisory_lock(787361902)"))
            try:
                subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)
                subprocess.run([sys.executable, "-m", "app.seed"], check=True)
            finally:
                connection.execute(text("SELECT pg_advisory_unlock(787361902)"))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
