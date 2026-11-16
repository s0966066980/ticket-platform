# 12: Independent load and consistency harness

Status: claimed
Triage: ready-for-agent
Blocked by: none

## Spec and ownership

Read ../spec.md, docs/api-contract.md, docs/acceptance-plan.md and /tmp/ticket-platform-load-notes.md. Ownership loadtests/**, sample_data/**, scripts/check_consistency.py and scripts/generate_load_data.py only. Build real 3000-user Locust and 2000-WebSocket harnesses, 100K import fixtures and 500K report fixture generation, deterministic SQL consistency checks and raw-result capture. Do not invent measured results. Tests at approved CLI/network public seams with real PostgreSQL when available; source compile/help checks distinguished from execution. Preserve other workers. Own worktree/branch based integration/mvp, merge latest tip before reporting SHA and evidence. Merger resolves ticket.

## Answer

Pending.
