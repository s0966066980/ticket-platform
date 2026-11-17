# 05: Events, publication and immutable pagination

Status: resolved
Triage: ready-for-agent
Blocked by: 04

## Spec

Read ../spec.md and referenced approved documents.

## Ownership and acceptance

backend/app/events.py, api/events.py, tests/test_events.py. CRUD ownership, optimistic edits, publish freeze, shared deletion/booking stream lock, fixed 10-minute snapshots and zone/seat reads. Use approved schema, public API tests.

Use tdd red-green vertical slices at approved public seams. You are not alone in the codebase: preserve others' changes. Implement in your own worktree/branch from integration/mvp; merge its latest tip before reporting. Do not mark this ticket resolved yourself; report commit, checks and remaining limitations to merger.

## Answer

Implemented activity CRUD with zone inputs, owner/role restrictions, optimistic If-Match edits, publication prerequisites and configuration freeze, coordinated soft deletion, fixed ten-minute materialized catalog snapshots, and coherent seat/version endpoints. All writes use the current request transaction and explicitly commit.

Reusable app.events helpers: get_event(session,event_id,principal,manage=False,lock=False), ensure_can_manage, lock_event_stream, event_json, seat_snapshot and event_version. get_event always issues SELECT with populate_existing=True; post-stream-lock booking checks should use default lock=False in READ COMMITTED so cached ORM state cannot hide deletion. lock=True uses events FOR NO KEY UPDATE and must be acquired before the stream lock only in activity management/import paths.

Public tests cover stale/concurrent edits, ownership, publication/freeze, snapshot invariance under insertion/edit/deletion, expiry, expired holds and order-history deletion rejection. Python compilation passed. Actual pytest remains unexecuted because uv dependency resolution fails DNS to pypi.org; no HTTP/PG result is claimed passed. Cross-feature delete/booking race remains part of ticket06/integrated A03 verification.

Merger evidence: ticket-05 merged normally with history preserved. Integration compileall and diff checks PASS. HTTP/PG16 behavior remains unverified due the recorded dependency/socket limits; closure covers completed source, not acceptance execution. Booking must use fresh get_event(..., lock=False) after lock_event_stream to detect concurrent deletion.
