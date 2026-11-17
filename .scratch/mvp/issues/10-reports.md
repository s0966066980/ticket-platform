# 10: Low-memory three-sheet Excel exports

Status: open
Triage: ready-for-agent
Blocked by: 04

## Spec

Read ../spec.md and referenced approved documents.

## Ownership and acceptance

backend/app/reports.py, api/reports.py, tests/test_reports.py. Permissions, consistent snapshots, numeric TWD, timezone, write-only temporary-file exports and memory-safe DB cursor.

Use tdd red-green vertical slices at approved public seams. You are not alone in the codebase: preserve others' changes. Implement in your own worktree/branch from integration/mvp; merge its latest tip before reporting. Do not mark this ticket resolved yourself; report commit, checks and remaining limitations to merger.

## Answer

Pending.

Dependency refinement: report implementation requires the approved schema and authentication/authorization, not Booking service code; it can start once ticket04 is integrated. Report verification still checks real order data at the public export seam.
