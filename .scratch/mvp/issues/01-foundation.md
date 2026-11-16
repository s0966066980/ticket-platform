# 01: Backend foundation and approved PostgreSQL schema

Status: open
Triage: ready-for-agent
Blocked by: none

## Spec

Read ../spec.md and referenced approved documents.

## Ownership and acceptance

backend/pyproject.toml, backend/app/config.py,database.py,models.py,errors.py,main.py,api/__init__.py, backend/alembic*, backend/tests/conftest.py. Build SQLAlchemy synchronous psycopg services and complete approved 24-table schema, migrations, health, dependency setup, seed support and real PG16 test harness. Define router registration for future modules without changing their ownership. No fabricated endpoints. Expose get_session and structured errors. HTTP test seam health and startup/migration commands.

Use tdd red-green vertical slices at approved public seams. You are not alone in the codebase: preserve others' changes. Implement in your own worktree/branch from integration/mvp; merge its latest tip before reporting. Do not mark this ticket resolved yourself; report commit, checks and remaining limitations to merger.

## Answer

Pending.
