# 01: Backend foundation and approved PostgreSQL schema

Status: claimed
Triage: ready-for-agent
Blocked by: none

## Spec

Read ../spec.md and referenced approved documents.

## Ownership and acceptance

backend/pyproject.toml, backend/app/config.py,database.py,models.py,errors.py,main.py,api/__init__.py, backend/alembic*, backend/tests/conftest.py. Build SQLAlchemy synchronous psycopg services and complete approved 24-table schema, migrations, health, dependency setup, seed support and real PG16 test harness. Define router registration for future modules without changing their ownership. No fabricated endpoints. Expose get_session and structured errors. HTTP test seam health and startup/migration commands.

Use tdd red-green vertical slices at approved public seams. You are not alone in the codebase: preserve others' changes. Implement in your own worktree/branch from integration/mvp; merge its latest tip before reporting. Do not mark this ticket resolved yourself; report commit, checks and remaining limitations to merger.

## Answer

Implemented source: 24 explicit SQLAlchemy models and frozen Alembic initial migration, config, sync session helpers, structured errors/request IDs, health/readiness endpoints, feature router registry, idempotent seed CLI and disposable PG16 test harness.

Verification: Python compileall passed. TDD health test was written before app source; execution initially failed because FastAPI was unavailable. Both pip and normal uv run pytest dependency resolution failed DNS to pypi.org. No HTTP or database integration test is represented as passing; root also established that this session cannot bind a PG16 socket. Migration/seed integration tests are supplied and require TEST_DATABASE_URL on a PostgreSQL16 server.

Interfaces: app.database.get_session yields sync Session with no implicit commit (services explicitly commit); new_session(), get_engine(), db_now(session). app.errors.AppError(status_code,code,message,details=None). Feature API modules export router (prefixed /api/v1) and optionally ws_router (no prefix). Missing feature modules are skipped only when find_spec returns None; internal import errors propagate. Settings environment names align with deployment ticket03. Runtime behavior remains to be verified when dependencies/services are accessible.
