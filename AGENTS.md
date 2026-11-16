# Project instructions

Read `.scratch/mvp/spec.md` before implementation and `docs/agents/domain.md` before domain exploration. The user approved the concrete design and schema; implement that design, and surface material changes to core schema before changing it.

## Code discovery
Prefer codebase-memory-mcp graph tools for code discovery; index the repository if needed. If `.codegraph/` exists, use `codegraph explore` before grep or code reads. For non-code strings/configs or insufficient graph results, use rg.

## Agent skills

### Issue tracker
Local Markdown tickets and dependency graph: read `docs/agents/issue-tracker.md` before claiming or closing work.

### Triage labels
Use `docs/agents/triage-labels.md` for the canonical role vocabulary.

### Domain docs
Single-context domain: read `GLOSSARY.md` and relevant `docs/adr/` before exploring or changing behavior.

## Verification
The approved test seams are HTTP API, WebSocket protocol, exported files, UI behavior, and startup/test commands in `docs/acceptance-plan.md`. Use tdd vertical slices at these seams; PostgreSQL 16 integration tests are required. Preserve real commit history and record only actually executed measurements. Other agents work concurrently; preserve their changes.
