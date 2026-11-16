# Issue tracker: Local Markdown

Specs live in `.scratch/<feature>/spec.md`; each ticket is `.scratch/<feature>/issues/NN-slug.md`.

`Status: open`, `claimed`, or `resolved` records execution lifecycle; `Triage:` records the canonical triage role. `Blocked by:` lists predecessor ticket numbers. A ticket is ready when all predecessors are resolved. Claim before starting; append evidence under `## Answer`, then set resolved only after integration merge and applicable verification. Comments append under `## Comments`.

The integration branch is `integration/mvp`. Implementers use individual branches/worktrees based on its current tip, merge its latest tip before reporting, and provide commit SHA and evidence. The merger integrates work and resolves tickets. Source delivery is the approved private GitHub repository; local tickets do not require a PR to close.
