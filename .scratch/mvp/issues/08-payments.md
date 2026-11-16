# 08: Durable mock gateway and payment reconciliation

Status: open
Triage: ready-for-agent
Blocked by: 06

## Spec

Read ../spec.md and referenced approved documents.

## Ownership and acceptance

backend/app/payments.py, app/gateway.py, api/payments.py, tests/test_payments.py. Signed callbacks, durable provider jobs, idempotent payment retries, late success reversal, recovery and real HTTP/PG tests.

Use tdd red-green vertical slices at approved public seams. You are not alone in the codebase: preserve others' changes. Implement in your own worktree/branch from integration/mvp; merge its latest tip before reporting. Do not mark this ticket resolved yourself; report commit, checks and remaining limitations to merger.

## Answer

Pending.
