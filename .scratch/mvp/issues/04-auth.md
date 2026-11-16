# 04: Session revocation and role authorization

Status: open
Triage: ready-for-agent
Blocked by: 01

## Spec

Read ../spec.md and referenced approved documents.

## Ownership and acceptance

backend/app/auth.py, api/auth.py, api/admin_users.py, tests/test_auth.py. Approved auth and role matrix, opaque token, real PostgreSQL HTTP tests. Coordinate central registration through existing foundation mechanism.

Use tdd red-green vertical slices at approved public seams. You are not alone in the codebase: preserve others' changes. Implement in your own worktree/branch from integration/mvp; merge its latest tip before reporting. Do not mark this ticket resolved yourself; report commit, checks and remaining limitations to merger.

## Answer

Pending.
