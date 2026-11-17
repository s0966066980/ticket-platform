# 11: Integrated verification, load evidence and final delivery

Status: resolved
Triage: ready-for-agent
Blocked by: 02, 03, 07, 08, 09, 10, 12, 13, 15

## Spec

Read ../spec.md and referenced approved documents.

## Ownership and acceptance

loadtests/**, sample_data/**, README.md, DESIGN.md, LOADTEST.md, AI_USAGE.md, docs/architecture.* and integration tests. Complete all acceptance checks possible, record actual limitations, cross-stack smoke/browser testing, complete load scripts and run specified scenarios when feasible; report concrete gaps rather than claim complete without evidence.

Use tdd red-green vertical slices at approved public seams. You are not alone in the codebase: preserve others' changes. Implement in your own worktree/branch from integration/mvp; merge its latest tip before reporting. Do not mark this ticket resolved yourself; report commit, checks and remaining limitations to merger.

## Answer

Resolved 2026-11-17 by the takeover merger. Evidence: full suite 113 backend + 5 frontend tests green locally on PostgreSQL 16 + Redis 7 and on GitHub Actions; `docker compose up -d --build` brings up all seven services; end-to-end smoke over Nginx (import → publish → WebSocket → booking → payment → cancel → report → admin); scenario A sold out 10,000 seats with 0 errors and a passing consistency audit; scenario B 2,000 subscribers with every change delivered (p50 255 ms); 100K CSV/JSON imports in ~15.7 s; 500K-order Excel under a 512 MiB single-process limit with 110 MiB peak RSS. Known gaps are listed in README.md.
