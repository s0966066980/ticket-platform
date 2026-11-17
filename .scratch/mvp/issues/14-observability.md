# 14: Independent observability pipeline

Status: resolved
Triage: ready-for-agent
Blocked by: 04

## Ownership and spec

backend/app/observability.py, backend/app/api/admin_metrics.py, backend/tests/test_observability.py and minimal middleware installation in backend/app/main.py. Read ../spec.md, docs/api-contract.md, docs/implementation-plan.md section8, core-schema and /tmp/ticket-platform-runtime-notes.md. Observability is independent of booking/import implementation;09 realtime/worker depends on this module. Implement durable request/service logs via Redis Streams AOF-aware handoff and spool fallback; PG batch-before-XACK dedup; instance/DB-clock-aware metrics, 60sec global aggregate (no averaged percentiles), business-vs-system failures, connection gauge, gaps and configured alerts, Admin metrics/alerts/request_id logs endpoints. Expose stable collector/connection update entry functions for09worker/feed. Include Nginx service-log ingestion and durable checkpoint if feasible in owned module. Do not import unmerged booking/feed modules. Minimal app.main middleware hook allowed; preserve factory/testing health behavior. You are not alone; do not modify booking/import/auth/errors/models. Use tdd at approved HTTP/CLI seams, currentbaselineCI confirmsPG16 tests run remotely. No invented performance or missingdata-as-zero. Mergelatestintegrationbeforecommit/report.

## Answer

Resolved 2026-11-17 by the takeover merger. Source: branch `ticket-14` (tip b9eb979, merged as cd77c41). Delivered the finished `backend/app/observability.py` (Redis Streams handoff with spool fallback, PG-before-XACK dedup, DB-anchored clock, per-source 1 s buckets, merged-sample percentiles, alert rules with min-request and sustain gates, Nginx log ingest), `backend/app/api/admin_metrics.py`, the `install_observability` hook in main.py and 23 tests. Nine genuine defects found in the earlier draft are recorded for AI_USAGE.md.

Evidence: full suite in the ticket worktree `111 passed` with Redis; merged suite `110 passed` locally. Compose-level verification is part of the delivery ticket.
