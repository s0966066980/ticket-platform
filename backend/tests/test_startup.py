"""Exercise the delivered database startup commands against real PostgreSQL16."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.integration
BACKEND = Path(__file__).parents[1]


def run_command(*arguments):
    return subprocess.run([sys.executable, *arguments], cwd=BACKEND, env=os.environ.copy(), text=True, capture_output=True, check=False)


def test_migrations_can_be_rebuilt_from_empty_database(test_database_url):
    downgraded = run_command('-m', 'alembic', 'downgrade', 'base')
    assert downgraded.returncode == 0, downgraded.stderr
    upgraded = run_command('-m', 'alembic', 'upgrade', 'head')
    assert upgraded.returncode == 0, upgraded.stderr
    current = run_command('-m', 'alembic', 'current')
    assert current.returncode == 0, current.stderr
    assert '0001_initial (head)' in current.stdout


def test_seed_command_is_repeatable(test_database_url):
    first = run_command('-m', 'app.seed')
    assert first.returncode == 0, first.stderr
    second = run_command('-m', 'app.seed')
    assert second.returncode == 0, second.stderr
    assert second.stdout.strip() == 'Configured seed accounts are present.'
