#!/usr/bin/env bash
# One entry point for the load-test stack and the four goal.md scenarios.
# Every run writes fixtures to loadtests/fixtures/<run>/ and raw evidence to
# loadtests/results/<run>/ (both gitignored: fixtures hold test credentials).
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
ENV_FILE="$ROOT/loadtests/load.env"
PROJECT=ticket-load
PY="$ROOT/.venv/bin/python"

usage() {
  cat <<'EOF'
Usage: scripts/loadtest.sh <command>

  up        build and start the isolated load stack (compose project ticket-load)
  status    show the load stack services
  down      stop the load stack and delete its volumes
  a         scenario A: 3,000 users buy from one 10,000-seat activity for 60 s, then audit
  b         scenario B: 2,000 WebSocket subscribers for 60 s with live seat changes, then audit
  import    100,000-row CSV and JSON seat imports, each within 60 s
  excel     500,000-order Excel export from one API process limited to 512 MiB
  all       up, then a, b, import and excel; exit status is non-zero if any scenario fails

The demo stack (docker compose up, port 8080) is not touched. Settings come from
loadtests/load.env (HTTP_PORT 8081, LOAD_DB_PORT 55433). Run scripts/setup.sh first.
EOF
}

# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a
BASE_URL="http://localhost:${HTTP_PORT}"
LOAD_DATABASE_URL="postgresql+psycopg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:${LOAD_DB_PORT}/${POSTGRES_DB}"
export LOAD_DATABASE_URL
COMPOSE=(docker compose -p "$PROJECT" --env-file "$ENV_FILE" -f "$ROOT/docker-compose.yml" -f "$ROOT/docker-compose.load.yml")
RUN_ID=${LOAD_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}
FIX="$ROOT/loadtests/fixtures/$RUN_ID"
RES="$ROOT/loadtests/results/$RUN_ID"

log() { printf '\n== %s\n' "$*"; }

require_python() {
  [[ -x "$PY" ]] || { echo 'Project Python environment missing. Run ./scripts/setup.sh first.' >&2; exit 2; }
  if ! "$PY" -c 'import locust, websockets, httpx, openpyxl, psycopg' 2>/dev/null; then
    log 'Installing load-test dependencies into .venv'
    "$PY" -m pip install -q -r "$ROOT/loadtests/requirements.txt"
  fi
}

wait_healthy() {
  for _ in $(seq 1 180); do
    if curl -fsS --max-time 2 "$BASE_URL/health" 2>/dev/null | grep -q '"status":"ok"'; then return 0; fi
    sleep 1
  done
  echo "Load stack did not become healthy at $BASE_URL" >&2
  "${COMPOSE[@]}" ps >&2 || true
  exit 1
}

prepare_run() {
  mkdir -p "$FIX" "$RES"
  chmod 700 "$FIX"
  git -C "$ROOT" rev-parse HEAD > "$RES/commit.txt" 2>/dev/null || echo unknown > "$RES/commit.txt"
  {
    date -u
    grep -E '^PRETTY_NAME=' /etc/os-release 2>/dev/null || true
    uname -srm
    nproc
    free -h | head -2
    lscpu 2>/dev/null | grep -E 'Model name' || true
    docker --version
    docker compose version
  } > "$RES/machine.txt" 2>&1
  "$PY" -m pip freeze > "$RES/dependencies.txt" 2>/dev/null || true
}

login() {  # login <base-url> <email> <password> -> bearer token on stdout
  curl -fsS -X POST "$1/api/v1/auth/login" -H 'Content-Type: application/json' \
    -d "{\"email\":\"$2\",\"password\":\"$3\"}" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["token"])'
}

event_of() { "$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["event_id"])' "$1"; }

audit() {  # audit <event-id> <output>
  "$PY" "$ROOT/scripts/check_consistency.py" --event-id "$1" --require-settled --output "$2" > /dev/null
}

sample_resources() {  # sample_resources <csv> <stop-file>: docker stats every ~2 s
  echo 'epoch,container,cpu_pct,mem_usage' > "$1"
  while [[ ! -f "$2" ]]; do
    now=$(date +%s.%N)
    docker stats --no-stream --format '{{.Name}},{{.CPUPerc}},{{.MemUsage}}' 2>/dev/null \
      | grep "^${PROJECT}-" | sed "s#^#${now},#; s#%,#,#; s# / [^,]*\$##" >> "$1" || true
  done
}

summary() {  # summary <python expression over d> <json>
  "$PY" -c "import json,sys; d=json.load(open(sys.argv[1])); print($1)" "$2"
}

scenario_a() {
  log "Scenario A: 3,000 users, 10,000 seats, 60 s steady state ($RES)"
  "$PY" "$ROOT/scripts/generate_load_data.py" booking --output "$FIX/booking.json" > /dev/null
  "$PY" "$ROOT/loadtests/prepare_sessions.py" --base-url "$BASE_URL" --fixture "$FIX/booking.json" \
    --output "$FIX/booking-sessions.json" --workers 32 > /dev/null
  ulimit -n 8192 2>/dev/null || true
  local status=0
  LOAD_FIXTURE="$FIX/booking-sessions.json" LOAD_RESULT="$RES/booking.json" \
    "$PY" -m locust -f "$ROOT/loadtests/locustfile.py" --headless --host "$BASE_URL" \
      --csv "$RES/booking" --csv-full-history --html "$RES/booking.html" --stop-timeout 20 \
      > "$RES/booking.log" 2>&1 || status=$?
  sleep 20  # let the worker settle expiries, payments and Outbox before auditing
  audit "$(event_of "$FIX/booking.json")" "$RES/booking-consistency.json" || status=1
  summary "{'ready_users': d['ready_users'], 'steady_seconds': round(d['steady_seconds'],1), **d['steady_statistics'], 'business': d['business_http_counts']}" "$RES/booking.json"
  summary "{'consistency_passed': d['passed'], 'violations': sum(d['violations'].values())}" "$RES/booking-consistency.json"
  [[ $(summary "d['required_scenario_completed']" "$RES/booking.json") == True ]] || status=1
  return $status
}

scenario_b() {
  log "Scenario B: 2,000 WebSocket subscribers, 60 s steady state ($RES)"
  "$PY" "$ROOT/scripts/generate_load_data.py" booking --users 2 --output "$FIX/ws.json" > /dev/null
  "$PY" "$ROOT/loadtests/prepare_sessions.py" --base-url "$BASE_URL" --fixture "$FIX/ws.json" \
    --output "$FIX/ws-sessions.json" --workers 2 > /dev/null
  ulimit -n 8192 2>/dev/null || true
  local status=0 stop="$RES/.stop-sampler"
  rm -f "$stop"
  sample_resources "$RES/websocket-resources.csv" "$stop" &
  local sampler=$!
  "$PY" "$ROOT/loadtests/websocket_benchmark.py" --base-url "$BASE_URL" --fixture "$FIX/ws-sessions.json" \
    --output "$RES/websocket.json" > "$RES/websocket.log" 2>&1 || status=$?
  touch "$stop"; wait "$sampler" 2>/dev/null || true; rm -f "$stop"
  sleep 15
  audit "$(event_of "$FIX/ws.json")" "$RES/websocket-consistency.json" || status=1
  summary "{k: d.get(k) for k in ('authenticated_connections','steady_seconds','resyncs','received_frames','delivery_samples','p50_ms','p95_ms','p99_ms','required_scenario_passed')}" "$RES/websocket.json"
  summary "{'consistency_passed': d['passed'], 'violations': sum(d['violations'].values())}" "$RES/websocket-consistency.json"
  return $status
}

scenario_import() {
  log "100K import: CSV and JSON ($RES)"
  "$PY" "$ROOT/scripts/generate_load_data.py" seats --output "$FIX/import" > /dev/null
  local token status=0 kind
  token=$(login "$BASE_URL" "${SEED_USER_EMAIL:-user@example.test}" "${SEED_USER_PASSWORD:-User-local-2026!}")
  for kind in csv json; do
    LOAD_TOKEN="$token" "$PY" "$ROOT/loadtests/import_benchmark.py" --base-url "$BASE_URL" \
      --file "$FIX/import/seats.$kind" --output "$RES/import-$kind.json" > "$RES/import-$kind.log" 2>&1 || status=$?
    summary "{'format': '$kind', 'total_seconds': round(d['total_seconds'],2), 'passed': d['required_scenario_passed']}" "$RES/import-$kind.json"
  done
  return $status
}

scenario_excel() {
  local port=${LIMITED_API_PORT:-8000} status=0
  log "500K-order Excel under a 512 MiB single API process on port $port ($RES)"
  if "$PY" -c 'import socket,sys; s=socket.socket(); sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) else 1)' "$port"; then :; else
    echo "Port $port is busy; set LIMITED_API_PORT to a free port." >&2; return 1
  fi
  "$PY" "$ROOT/scripts/generate_load_data.py" report --output "$FIX/report.json" > /dev/null
  mkdir -p "$RES/limited-api"
  # Telemetry goes to a local spool: this process measures memory, not Redis.
  DATABASE_URL="$LOAD_DATABASE_URL" REDIS_URL='redis://127.0.0.1:1/0' \
    TELEMETRY_SPOOL_DIR="$RES/limited-api/telemetry" UPLOAD_DIR="$RES/limited-api/uploads" PYTHONPATH="$ROOT/backend" \
    "$PY" "$ROOT/loadtests/limited_api.py" --port "$port" > "$RES/limited-api/api.log" 2>&1 &
  local api=$!  # limited_api.py execs uvicorn, so this PID is the measured server process
  for _ in $(seq 1 60); do curl -fsS --max-time 2 "http://127.0.0.1:$port/health" > /dev/null 2>&1 && break; sleep 1; done
  local token
  token=$(login "http://127.0.0.1:$port" "${SEED_ADMIN_EMAIL:-admin@example.test}" "${SEED_ADMIN_PASSWORD:-Admin-local-2026!}") || status=1
  if ((status == 0)); then
    LOAD_TOKEN="$token" "$PY" "$ROOT/loadtests/report_benchmark.py" --base-url "http://127.0.0.1:$port" \
      --event-id "$(event_of "$FIX/report.json")" --api-pid "$api" --output "$RES/report.json" > "$RES/report.log" 2>&1 || status=$?
  fi
  kill "$api" 2>/dev/null || true
  wait "$api" 2>/dev/null || true
  [[ -f "$RES/report.json" ]] || return 1
  summary "{k: d.get(k) for k in ('single_process','enforced_address_space_hard_limit_bytes','orders_rows','timezone_in_headers','download_seconds','peak_api_rss_bytes','required_scenario_passed')}" "$RES/report.json"
  return $status
}

up() {
  log "Starting load stack ($PROJECT) on $BASE_URL, PostgreSQL on 127.0.0.1:$LOAD_DB_PORT"
  "${COMPOSE[@]}" up -d --build
  wait_healthy
  "${COMPOSE[@]}" ps
}

case "${1:-}" in
  -h|--help|help|'') usage; [[ -n ${1:-} ]] || exit 2 ;;
  up) up ;;
  status) "${COMPOSE[@]}" ps ;;
  down) "${COMPOSE[@]}" down -v ;;
  a|b|import|excel)
    require_python; wait_healthy; prepare_run
    case "$1" in a) scenario_a ;; b) scenario_b ;; import) scenario_import ;; excel) scenario_excel ;; esac
    ;;
  all)
    require_python; up; prepare_run
    failed=()
    scenario_a || failed+=(A)
    scenario_b || failed+=(B)
    scenario_import || failed+=(import)
    scenario_excel || failed+=(excel)
    log "Evidence in $RES"
    if ((${#failed[@]})); then echo "FAILED: ${failed[*]}" >&2; exit 1; fi
    echo 'All four scenarios passed.'
    ;;
  *) echo "Unknown command: $1" >&2; usage >&2; exit 2 ;;
esac
