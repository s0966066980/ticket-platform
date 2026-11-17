# Reproducible load and export checks

These are executable harnesses, **not measured results**; measured numbers live in `../LOADTEST.md`. Never replace the required scenarios with smaller smoke settings and claim full acceptance.

## Quick start

```bash
PYTHON_BIN=python3.13 ./scripts/setup.sh   # once: project .venv
./scripts/loadtest.sh all                  # load stack + scenarios A, B, import, Excel
./scripts/loadtest.sh down                 # remove the load stack and its data
```

`scripts/loadtest.sh` installs `loadtests/requirements.txt` into `.venv` when needed, starts the isolated compose project `ticket-load`, and runs each scenario exactly as documented below. Single scenarios: `./scripts/loadtest.sh up`, then `a`, `b`, `import` or `excel`. Each run writes:

- `loadtests/fixtures/<run>/`: generated fixtures and authenticated session manifests. They contain test credentials and bearer tokens (directory mode 0700, files 0600) and are gitignored; never publish them.
- `loadtests/results/<run>/`: raw evidence (`commit.txt`, `machine.txt`, `dependencies.txt`, Locust CSV/HTML, benchmark JSON, delivery logs, resource samples, consistency audits). Gitignored; quote numbers from here into `LOADTEST.md`.

## The load stack

| Item | Value (from `loadtests/load.env`) |
| --- | --- |
| Compose | `docker compose -p ticket-load --env-file loadtests/load.env -f docker-compose.yml -f docker-compose.load.yml` |
| Public URL | `http://localhost:8081` (the demo stack keeps 8080) |
| PostgreSQL on the host | `127.0.0.1:55433`, database `ticket_platform_load`, user `ticket` |
| `LOAD_DATABASE_URL` used by fixtures and audits | `postgresql+psycopg://ticket:ticket-load-only@127.0.0.1:55433/ticket_platform_load` |

API, worker and gateway all use that one disposable database. Fixture generation writes directly with SQL (it refuses databases not named `ticket_test*` or `*_load`) and never deletes existing data: each invocation uses a fresh namespace, so runs can repeat on the same stack. Direct SQL fixtures are setup, not API correctness evidence.

## What each scenario does

The script's steps, for running them by hand (`BASE=http://localhost:8081`, `F` and `R` are the fixture and result directories, `LOAD_DATABASE_URL` exported as above).

### A: 3,000 ready users for 60 seconds

```bash
.venv/bin/python scripts/generate_load_data.py booking --output $F/booking.json
.venv/bin/python loadtests/prepare_sessions.py --base-url $BASE --fixture $F/booking.json --output $F/booking-sessions.json --workers 32
LOAD_FIXTURE=$F/booking-sessions.json LOAD_RESULT=$R/booking.json \
  .venv/bin/python -m locust -f loadtests/locustfile.py --headless --host $BASE \
  --csv $R/booking --csv-full-history --html $R/booking.html --stop-timeout 20
sleep 20
.venv/bin/python scripts/check_consistency.py --event-id <event_id from booking.json> --require-settled --output $R/booking-consistency.json
```

The load shape ramps at 100 users/s, waits until all 3,000 preauthenticated users finished `on_start`, resets Locust statistics, then holds all users for at least 60 monotonic seconds (warmup capped at 180 s). Do **not** add `--run-time`, which would include ramp-up. Each account repeatedly buys one ticket (70% zone allocation, 30% named seat) and pays with a successful mock payment, 50–200 ms think time, under the real 4-ticket quota. Expected `409` business outcomes are recorded by code in `business_http_counts` and excluded from Locust failures; network errors and 5xx are failures and retry with the same Idempotency-Key. The single-process harness rejects distributed Locust to avoid sharing accounts.

The audit runs in a read-only repeatable-read transaction: duplicate sold seats, order totals/counts, paid seat/payment ownership, active holds, effective quotas, event versions and Outbox. With `--require-settled`, unpublished Outbox rows and pending reversals are failures.

### B: 2,000 subscribers plus real seat changes

```bash
.venv/bin/python scripts/generate_load_data.py booking --users 2 --output $F/ws.json
.venv/bin/python loadtests/prepare_sessions.py --base-url $BASE --fixture $F/ws.json --output $F/ws-sessions.json --workers 2
ulimit -n 8192
.venv/bin/python loadtests/websocket_benchmark.py --base-url $BASE --fixture $F/ws-sessions.json --output $R/websocket.json
sleep 15
.venv/bin/python scripts/check_consistency.py --event-id <event_id from ws.json> --require-settled --output $R/websocket-consistency.json
```

2,000 sockets authenticate (first frame `{type:auth}`) before measurement; the first fixture account then reserves and cancels one seat repeatedly for 60 s. A change fails unless every subscriber sees it within 10 s; disconnects also fail the scenario. Each delivery is logged in `websocket.deliveries.jsonl`; latency is HTTP mutation start to observed frame on the client's monotonic clock (nearest-rank percentiles). The script also samples `docker stats` for every `ticket-load-*` container into `websocket-resources.csv` while the benchmark runs.

### 100K import

```bash
.venv/bin/python scripts/generate_load_data.py seats --output $F/import
LOAD_TOKEN=<user token> .venv/bin/python loadtests/import_benchmark.py --base-url $BASE --file $F/import/seats.csv --output $R/import-csv.json
LOAD_TOKEN=<user token> .venv/bin/python loadtests/import_benchmark.py --base-url $BASE --file $F/import/seats.json --output $R/import-json.json
```

Each run creates its own draft activity, uploads, polls the job every 500 ms and passes only with 100,000 inserted rows in at most 60 s.

### 500K-order Excel within a 512 MiB API process

```bash
.venv/bin/python scripts/generate_load_data.py report --output $F/report.json
DATABASE_URL=$LOAD_DATABASE_URL REDIS_URL=redis://127.0.0.1:1/0 PYTHONPATH=backend \
  .venv/bin/python loadtests/limited_api.py --port 8000 &
API_PID=$!
LOAD_TOKEN=<admin token from http://127.0.0.1:8000> .venv/bin/python loadtests/report_benchmark.py \
  --base-url http://127.0.0.1:8000 --event-id <event_id from report.json> --api-pid $API_PID --output $R/report.json
kill $API_PID
```

`limited_api.py` execs a single uvicorn process (no `--workers`, which would fork a child and measure the wrong PID) under a hard 512 MiB **address-space** limit, stricter than resident memory. The client samples that PID's VmRSS every 20 ms, verifies the enforced limit and that there are no child processes, streams the download to disk and checks Summary/Zones/Orders, 500,000 order rows, numeric money and `Asia/Taipei` headers. Telemetry is spooled locally (Redis deliberately unreachable) so the process measures the export only. Set `LIMITED_API_PORT` for the script if port 8000 is busy.

Fixtures contain 500,000 one-ticket orders, mostly historical CANCELED/EXPIRED plus a bounded PAID subset (at most four paid tickets per user, one paid owner per seat), priced 100 TWD + 5 TWD fee.

## Evidence boundary and sources

CLI/fixture tests: `python3 -m unittest discover -s loadtests/tests -v`. They validate generation and command interfaces, not network performance. Unavailable Docker or missing dependencies must be recorded as unexecuted scenarios, never passing results. No benchmark numbers are committed here.

- [Locust API](https://docs.locust.io/en/stable/api.html) and [headless execution](https://docs.locust.io/en/stable/running-without-web-ui.html).
- [websockets asyncio client](https://websockets.readthedocs.io/en/stable/reference/asyncio/client.html).
- [OpenPyXL optimized modes](https://openpyxl.readthedocs.io/en/stable/optimized.html).
