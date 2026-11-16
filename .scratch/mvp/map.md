# MVP task graph

01 foundation -> 04 auth -> 05 events -> 06 booking -> 08 payments / 10 reports
05 events -> 07 imports
06 booking + 07 imports -> 09 realtime_metrics
02 frontend + 03 deployment + 07 + 08 + 09 + 10 -> 11 delivery

Initial frontier: 01, 02, 03. Closure requires merge and evidence.

## Decisions-so-far

- Ticket 03 deployment source integrated and public CLI/config checks passed; runtime validation remains explicitly pending. See [ticket](issues/03-deployment.md).
