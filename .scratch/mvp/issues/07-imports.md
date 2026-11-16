# 07: Recoverable CSV and JSON import

Status: open
Triage: ready-for-agent
Blocked by: 05

## Spec

Read ../spec.md and referenced approved documents.

## Ownership and acceptance

backend/app/imports.py, api/imports.py, tests/test_imports.py. Streaming file limits, SHA dedup, both modes, complete staging/checkpoints, errors, authorized acceptance, generation fencing and background processing entry function.

Use tdd red-green vertical slices at approved public seams. You are not alone in the codebase: preserve others' changes. Implement in your own worktree/branch from integration/mvp; merge its latest tip before reporting. Do not mark this ticket resolved yourself; report commit, checks and remaining limitations to merger.

## Answer

Pending.
