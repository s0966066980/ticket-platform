# MVP task graph

01 foundation -> 04 auth -> 05 events -> 06 booking -> 08 payments
05 events -> 07 imports
06 booking + 07 imports -> 09 realtime_metrics
02 frontend + 03 deployment + 07 + 08 + 09 + 10 -> 11 delivery

Initial frontier: 01, 02, 03. Closure requires merge and evidence.

## Decisions-so-far

- Ticket 03 deployment source integrated and public CLI/config checks passed; runtime validation remains explicitly pending. See [ticket](issues/03-deployment.md).

12 load_harness is independent of implementation and now claimed; 11 delivery also depends on 12.

- Ticket 01 backend foundation integrated; compile/static model-migration columns PASS, runtime dependency/PG16 checks explicitly pending. [Ticket](issues/01-foundation.md). Unlocks ticket04 auth.

- Ticket 02 UI source integrated; four behavior tests, TypeScript and production build passed in integration checkout. Visual/full-backend E2E remains pending. [Ticket](issues/02-frontend.md).

- Ticket04 session/auth source integrated with compile/diff checks; HTTP/PG execution remains pending. [Ticket](issues/04-auth.md).
- Frontier refinement: 04 auth -> 05 events and 10 reports. Reports depends on schema+auth rather than Booking implementation.

13 CI depends01,02,03,04 (resolved); final delivery also depends13. Configure real PG16/Redis CI and report actual result when available.

- Ticket05 events/snapshot source integrated; compile/diff PASS, HTTP/PG execution pending. Unlocks 06 booking and 07 imports. [Ticket](issues/05-events.md).
- Ticket12 load harness integrated; two load CLI plus four startup CLI checks passed, and load CLI discovery joins run_tests.sh. Full-scale measurements remain pending. [Ticket](issues/12-load_harness.md).

- Ticket10 report source integrated; compile/diff PASS, PostgreSQL/HTTP/XLSX tests and 500K memory measurement remain pending. [Ticket](issues/10-reports.md).

- Ticket13 CI source integrated; three gate checks, YAML parse and compile/diff PASS. Remote backend/frontend result must be read from GitHub run status. [Ticket](issues/13-ci.md).
