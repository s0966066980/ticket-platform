# 08: Durable mock gateway and payment reconciliation

Status: resolved
Triage: ready-for-agent
Blocked by: 06

## Spec

Read ../spec.md and referenced approved documents.

## Ownership and acceptance

backend/app/payments.py, app/gateway.py, api/payments.py, tests/test_payments.py. Signed callbacks, durable provider jobs, idempotent payment retries, late success reversal, recovery and real HTTP/PG tests.

Use tdd red-green vertical slices at approved public seams. You are not alone in the codebase: preserve others' changes. Implement in your own worktree/branch from integration/mvp; merge its latest tip before reporting. Do not mark this ticket resolved yourself; report commit, checks and remaining limitations to merger.

## Answer

Resolved 2026-11-17 by the takeover merger. Source: branch `ticket-08` (feature tip 79bba81, merged as fcd9c43 into integration/mvp). Delivered `backend/app/api/payments.py`, `backend/app/gateway.py`, finished `backend/app/payments.py`, 25 tests in `backend/tests/test_payments.py` at the HTTP + PostgreSQL 16 seams (initiation idempotency, signed callbacks, late/extra success reversal, gateway durability, worker charge/reversal/reconciliation, two concurrent workers).

Evidence: full backend suite in the ticket worktree against local PostgreSQL 16 + Redis 7: `88 passed` (includes feed/worker tests). Two genuine defects found in the earlier draft (500 instead of 503 on DB failure; reversal replay overwrote reversed_at) are recorded for AI_USAGE.md. Multi-process compose operation verified separately by the delivery ticket.
