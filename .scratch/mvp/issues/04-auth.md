# 04: Session revocation and role authorization

Status: claimed
Triage: ready-for-agent
Blocked by: 01

## Spec

Read ../spec.md and referenced approved documents.

## Ownership and acceptance

backend/app/auth.py, api/auth.py, api/admin_users.py, tests/test_auth.py. Approved auth and role matrix, opaque token, real PostgreSQL HTTP tests. Coordinate central registration through existing foundation mechanism.

Use tdd red-green vertical slices at approved public seams. You are not alone in the codebase: preserve others' changes. Implement in your own worktree/branch from integration/mvp; merge its latest tip before reporting. Do not mark this ticket resolved yourself; report commit, checks and remaining limitations to merger.

## Answer

Implemented opaque-token login/logout/register/me, database-backed active/auth-version validation, reusable immutable Principal and role/ownership helpers, Admin user listing and role/active updates. Registration rejects privilege fields. Seed Argon2 hashes are directly compatible. Each test HTTP request now receives its own Session for later concurrent booking tests.

Validation: source compilation passed. Public HTTP tests cover login/logout, expiry, privilege injection, normalized duplicates, role matrix, and immediate invalidation of multiple sessions. Actual pytest execution remains blocked by PyPI DNS dependency download; no HTTP/PG test is claimed passed. The schema is unchanged.
