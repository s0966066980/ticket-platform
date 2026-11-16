# 06: Atomic reservations, quota, cancellation and idempotency

Status: open
Triage: ready-for-agent
Blocked by: 05

## Spec

Read ../spec.md and referenced approved documents.

## Ownership and acceptance

backend/app/booking.py, app/idempotency.py, api/bookings.py, api/orders.py, tests/test_booking.py. Full transactional flow, quota mutex, expiry-aware seat reclaim, region DB allocation, DB clock, prices, outbox, concurrent PG16 tests and idempotency leases.

Use tdd red-green vertical slices at approved public seams. You are not alone in the codebase: preserve others' changes. Implement in your own worktree/branch from integration/mvp; merge its latest tip before reporting. Do not mark this ticket resolved yourself; report commit, checks and remaining limitations to merger.

## Answer

Pending.
