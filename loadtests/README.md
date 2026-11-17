# Reproducible load and export checks

These are executable harnesses, **not measured results**. Run against an isolated PostgreSQL 16 database with migrated schema and the complete API/worker/gateway/Redis stack. Record API-server and load-generator CPU/RAM/OS separately, container/resource limits, raw outputs and the commit SHA beside results; never replace the required scenarios with smaller smoke settings and claim full acceptance.

Install harness dependencies into the project venv:

```bash
.venv/bin/python -m pip install -r loadtests/requirements.txt
mkdir -p loadtests/results loadtests/fixtures
git rev-parse HEAD > loadtests/results/commit.txt
.venv/bin/python -m pip freeze > loadtests/results/dependencies.txt
```

`LOAD_DATABASE_URL` must refer to a disposable database named `ticket_test*` or `*_load` for fixture generation. Provide it through environment, never as a command-line password. Run Alembic migrations on that database before generating fixtures. No existing data is deleted; each invocation uses a fresh namespace. Fixtures and sessions are private files (mode 0600), excluded from Git. They contain test credentials or bearer tokens and must not be published in reports.

## A: 3000 ready users for 60 seconds

Prepare 3000 distinct users and one published 10000-seat event. Direct SQL fixture setup is distinct from API correctness evidence. API, worker and gateway must all point at the same disposable DB.

```bash
export LOAD_DATABASE_URL='postgresql+psycopg://ticket_test@127.0.0.1:55432/ticket_test_load'
.venv/bin/python scripts/generate_load_data.py booking --output loadtests/fixtures/booking.json
.venv/bin/python loadtests/prepare_sessions.py --base-url http://localhost:8080 --fixture loadtests/fixtures/booking.json --output loadtests/fixtures/booking-sessions.json
LOAD_FIXTURE=loadtests/fixtures/booking-sessions.json LOAD_RESULT=loadtests/results/booking.json \
  .venv/bin/python -m locust -f loadtests/locustfile.py --headless --host http://localhost:8080 \
  --csv loadtests/results/booking --csv-full-history --html loadtests/results/booking.html --stop-timeout 20
```

The load shape ramps at 100 users/sec, waits for all 3000 preauthenticated users to finish on_start, resets Locust statistics, then holds all users for at least 60 monotonic seconds. Warmup has a 180-second cap. Do **not** add a 60-second `--run-time`, which would include ramp-up. The summary states actual ready count and steady interval. Raw CSV history includes warmup rows before the statistics reset; keep phase timing from the summary when analyzing it. This harness intentionally uses one Locust process; distributed execution requires explicit account partitioning and is rejected rather than sharing user accounts accidentally.

Each account continuously buys one ticket, 70% by area and 30% by specified seat, and initiates a successful mock payment after a booking. Every account is subject to the real 4-ticket quota; 3000 accounts have a theoretical maximum of 12000 tickets but only 10000 seats exist. Later quota/seat-contention 409 responses are expected, retained by code in business_http_counts. They are excluded from Locust infrastructure failure counts; both classes must appear in the final report. Network/5xx retries reuse the original action key. There is 50–200ms client think time.

After workers converge, audit using the fixture's event_id:

```bash
.venv/bin/python scripts/check_consistency.py --event-id EVENT_UUID --require-settled --output loadtests/results/booking-consistency.json
```

The audit runs in a read-only repeatable-read transaction and reports duplicate sold seats, order totals/counts, paid seat/payment ownership, active holds, effective user quotas and event/outbox consistency. Expired-but-not-cleaned records are informational because lease cleanup is asynchronous. Unpublished outbox and pending reversals become failures with `--require-settled`.

## B: 2000 subscribers plus real mutations

Use a **fresh booking fixture** and authenticated session manifest. Increase the client process descriptor limit to at least 2256. The subscriber account can be shared because the requirement concerns concurrent connections; the mutating account is its first fixture user. It repeatedly reserves and cancels one available seat to avoid exhausting its quota.

```bash
ulimit -n 4096
.venv/bin/python loadtests/websocket_benchmark.py --base-url http://localhost:8080 \
  --fixture loadtests/fixtures/ws-sessions.json --output loadtests/results/websocket.json
```

2000 sockets authenticate before measurement. One consistent baseline snapshot is shared by the harness while each connection separately receives/validates versions; gap recovery fetches a fresh HTTP snapshot. The steady interval is 60 seconds and includes reserve/cancel mutations. A phase fails unless all subscribers see the corresponding change within 10 seconds; disconnects also fail the scenario. Each observed delivery is stored in `.deliveries.jsonl` with connection, phase, version and monotonic latency. Latency is mutation HTTP initiation to observed WebSocket frame, including API processing; p50/p95/p99 use nearest-rank, not server-side timestamps. Gaps/resyncs and failed delivery counts are explicit. The 5-second version heartbeat is a protocol feature, not a promise of recovery during network outages.

## 100K import

```bash
.venv/bin/python scripts/generate_load_data.py seats --output loadtests/fixtures/import
# LOAD_TOKEN is an authorized creator token obtained before measurement.
.venv/bin/python loadtests/import_benchmark.py --base-url http://localhost:8080 \
  --file loadtests/fixtures/import/seats.csv --output loadtests/results/import.json
```

The benchmark creates a fresh draft event with zone A, uploads the file, then polls the real job every 500ms. It records upload/accept time, every raw job response, acceptance-to-observed-finish and total elapsed. Success requires 100000 inserted rows and total ≤60 seconds; acceptance-to-finish is separately available. Test JSON by running again with the generated JSON file; each run creates its own event. Sample small valid and invalid files live under sample_data/.

## 500K-order Excel within a 512MiB API instance

```bash
.venv/bin/python scripts/generate_load_data.py report --output loadtests/fixtures/report.json
# Set DATABASE_URL to the same disposable DB, plus normal app environment.
PYTHONPATH=backend .venv/bin/python loadtests/limited_api.py --port 8000 &
API_PID=$!
# LOAD_TOKEN must belong to the report event owner or an Admin/Viewer on this DB.
.venv/bin/python loadtests/report_benchmark.py --base-url http://localhost:8000 \
  --event-id EVENT_UUID --api-pid "$API_PID" --output loadtests/results/report.json
```

`limited_api.py` runs exactly one API process with a hard **address-space** ceiling of 512MiB, stricter than resident memory. This may expose allocation/threads limits that a container RSS limit does not; document that distinction in results. The client records actual peak API VmRSS from /proc every 20ms, checks the enforced limit, streams the download to disk, and scans the workbook in read-only mode. It verifies Summary/Zones/Orders, 500000 order rows and Asia/Taipei headers. Do not point it at an unrestricted API PID and claim a memory-limit pass. Container/cgroup execution is another valid method but requires its own complete API-instance memory evidence.

Fixtures contain 500000 one-ticket orders, mostly historical CANCELED/EXPIRED, plus a bounded PAID subset: at most four paid tickets per user and one paid owner per seat. Canceled/expired historical items may reuse seats; sold items cannot. Prices are 100 TWD + 5 TWD service fee. This gives nonzero sales and historical rows without violating quota or inventing 500000 sold seats in a 10000-seat event. Initial fixture state has event version zero and no fabricated transition history.

## Evidence boundary and sources

CLI/fixture tests: `python3 -m unittest discover -s loadtests/tests -v`. They validate generation and command interfaces, not network performance. Socket restrictions, unavailable Docker daemon or missing dependencies must be recorded as unexecuted scenarios, never passing results. No benchmark numbers are committed here.

- [Locust API](https://docs.locust.io/en/stable/api.html) and [headless execution](https://docs.locust.io/en/stable/running-without-web-ui.html).
- [websockets asyncio client](https://websockets.readthedocs.io/en/stable/reference/asyncio/client.html).
- [OpenPyXL optimized modes](https://openpyxl.readthedocs.io/en/stable/optimized.html).
