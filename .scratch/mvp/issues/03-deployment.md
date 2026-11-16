# 03: Compose and development execution tooling

Status: claimed
Triage: ready-for-agent
Blocked by: none

## Spec

Read ../spec.md and referenced approved documents.

## Ownership and acceptance

docker-compose.yml, nginx/**, scripts/**, .env.example, root README.md initial. Seven approved services; images backend app.main:app, app.gateway:app, python -m app.worker, frontend build. PG16 host test helper, one-command test runner and source isolation; no backend or frontend changes. Build local tooling and compose config validation, real Docker limitation documented.

Use tdd red-green vertical slices at approved public seams. You are not alone in the codebase: preserve others' changes. Implement in your own worktree/branch from integration/mvp; merge its latest tip before reporting. Do not mark this ticket resolved yourself; report commit, checks and remaining limitations to merger.

## Answer

Implemented deployment configuration and public CLI helpers. Evidence: docker compose config --quiet PASS; four CLI contract tests PASS after observed initial red failures; bash -n and git diff --check PASS. PG16 helper actual start attempted and failed on sandbox socket bind; Docker runtime/build not executed because daemon socket access unavailable. README documents these limits. Runtime files and migrations depend on the other approved tickets; merger closes this ticket after integration.

Shared runtime contracts: app.seed CLI; app.gateway GET /health; worker python -m app.worker; pip package .[test]; NGINX_LOG_PATH is persistent JSON proxy service logs, not a second canonical API metrics source.
