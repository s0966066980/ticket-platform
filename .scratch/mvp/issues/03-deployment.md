# 03: Compose and development execution tooling

Status: open
Triage: ready-for-agent
Blocked by: none

## Spec

Read ../spec.md and referenced approved documents.

## Ownership and acceptance

docker-compose.yml, nginx/**, scripts/**, .env.example, root README.md initial. Seven approved services; images backend app.main:app, app.gateway:app, python -m app.worker, frontend build. PG16 host test helper, one-command test runner and source isolation; no backend or frontend changes. Build local tooling and compose config validation, real Docker limitation documented.

Use tdd red-green vertical slices at approved public seams. You are not alone in the codebase: preserve others' changes. Implement in your own worktree/branch from integration/mvp; merge its latest tip before reporting. Do not mark this ticket resolved yourself; report commit, checks and remaining limitations to merger.

## Answer

Pending.
