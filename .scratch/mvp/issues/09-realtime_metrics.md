# 09: Workers, seat synchronization and observability

Status: resolved
Triage: ready-for-agent
Blocked by: 06, 07, 08, 14

## Spec

Read ../spec.md and referenced approved documents.

## Ownership and acceptance

backend/app/worker.py, backend/app/feed.py, backend/app/api/feed.py, backend/tests/test_feed.py, plus the existing frontend event-feed hook needed to honor resync_required. Workers integrate approved booking/import/payment/observability service entry points, DB Outbox publication and reclaims, DB-authoritative event versions, Redis Pub/Sub, bounded WebSocket queues, signed-in session checks, snapshot handshake and resync. Observability.py/admin_metrics.py/tests belong to ticket14; use its collector APIs rather than reimplementing them. See /tmp/ticket-platform-runtime-notes.md for bulk import resync marker and Nginx trace input.

Use tdd red-green vertical slices at approved public seams. You are not alone in the codebase: preserve others' changes. Implement in your own worktree/branch from integration/mvp; merge its latest tip before reporting. Do not mark this ticket resolved yourself; report commit, checks and remaining limitations to merger.

## Answer

Resolved 2026-11-17 by the takeover merger. Source: branch `ticket-09` (tip 1716aef, merged as 953c8f8). Delivered `backend/app/feed.py` (ordered Outbox publication under a per-activity advisory lock, Redis Pub/Sub fan-out, bounded per-connection queues closed with a resync code, batch session re-verification and DB version heartbeat every WS_VERSION_CHECK_SECONDS), `backend/app/api/feed.py`, `backend/app/worker.py` (threaded idempotent job groups, `--once`), frontend `resync_required` handling with a vitest, 8 feed tests and 3 worker tests on PostgreSQL 16 + Redis 7.

Evidence: local full suite green; compose end-to-end smoke over Nginx observed seat_changed versions 2→3→4 and heartbeats on a live socket; worker SIGKILL between booking commit and publication converged 3.6 s after restart with no manual action.

Observability source moved to independent ticket14;09 integrates its worker collector/middleware API and owns any needed frontend resync_required handling.
