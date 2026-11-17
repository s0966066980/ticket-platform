# 09: Workers, seat synchronization and observability

Status: open
Triage: ready-for-agent
Blocked by: 06, 07, 08, 14

## Spec

Read ../spec.md and referenced approved documents.

## Ownership and acceptance

backend/app/worker.py, backend/app/feed.py, backend/app/api/feed.py, backend/tests/test_feed.py, plus the existing frontend event-feed hook needed to honor resync_required. Workers integrate approved booking/import/payment/observability service entry points, DB Outbox publication and reclaims, DB-authoritative event versions, Redis Pub/Sub, bounded WebSocket queues, signed-in session checks, snapshot handshake and resync. Observability.py/admin_metrics.py/tests belong to ticket14; use its collector APIs rather than reimplementing them. See /tmp/ticket-platform-runtime-notes.md for bulk import resync marker and Nginx trace input.

Use tdd red-green vertical slices at approved public seams. You are not alone in the codebase: preserve others' changes. Implement in your own worktree/branch from integration/mvp; merge its latest tip before reporting. Do not mark this ticket resolved yourself; report commit, checks and remaining limitations to merger.

## Answer

Pending.

Observability source moved to independent ticket14;09 integrates its worker collector/middleware API and owns any needed frontend resync_required handling.
