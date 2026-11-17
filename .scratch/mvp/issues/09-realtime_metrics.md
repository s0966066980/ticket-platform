# 09: Workers, seat synchronization and observability

Status: open
Triage: ready-for-agent
Blocked by: 06, 07, 14

## Spec

Read ../spec.md and referenced approved documents.

## Ownership and acceptance

backend/app/worker.py, app/feed.py, app/observability.py, api/feed.py, api/admin_metrics.py, tests/test_feed.py, tests/test_observability.py. Workers integrate approved service entry points, DB outbox, ordered versions, Redis and stream logs, bounded WS queues and resync, complete metrics/alerts/tracing.

Use tdd red-green vertical slices at approved public seams. You are not alone in the codebase: preserve others' changes. Implement in your own worktree/branch from integration/mvp; merge its latest tip before reporting. Do not mark this ticket resolved yourself; report commit, checks and remaining limitations to merger.

## Answer

Pending.

Observability source moved to independent ticket14;09 integrates its worker collector/middleware API and owns any needed frontend resync_required handling.
