# 05: Events, publication and immutable pagination

Status: open
Triage: ready-for-agent
Blocked by: 04

## Spec

Read ../spec.md and referenced approved documents.

## Ownership and acceptance

backend/app/events.py, api/events.py, tests/test_events.py. CRUD ownership, optimistic edits, publish freeze, shared deletion/booking stream lock, fixed 10-minute snapshots and zone/seat reads. Use approved schema, public API tests.

Use tdd red-green vertical slices at approved public seams. You are not alone in the codebase: preserve others' changes. Implement in your own worktree/branch from integration/mvp; merge its latest tip before reporting. Do not mark this ticket resolved yourself; report commit, checks and remaining limitations to merger.

## Answer

Pending.
