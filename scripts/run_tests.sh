#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
MODE=all
case "${1:-}" in
  --help|-h)
    echo 'Usage: scripts/run_tests.sh [--backend-only|--frontend-only]'
    echo 'Runs CLI contracts, backend tests on isolated PostgreSQL 16, frontend tests and build.'
    echo 'Run scripts/setup.sh first. Set TEST_DATABASE_URL only for an explicitly disposable ticket_test* database.'
    echo 'The isolated PostgreSQL uses the first free port from 55432 unless PG_TEST_PORT is set.'
    echo 'Without REDIS_URL a disposable Redis 7 container is started and removed (needs Docker);'
    echo 'if Docker is unavailable the Redis-dependent tests are reported as skipped.'
    exit 0 ;;
  --backend-only) MODE=backend ;;
  --frontend-only) MODE=frontend ;;
  '') ;;
  *) echo "Unknown argument: $1" >&2; exit 2 ;;
esac
[[ $# -le 1 ]] || { echo 'Too many arguments.' >&2; exit 2; }
CLEANUPS=()
cleanup() {
  local step
  for step in "${CLEANUPS[@]}"; do eval "$step" || true; done
}
trap cleanup EXIT

if [[ "$MODE" != frontend ]]; then
  PYTHON=${PYTHON:-"$ROOT/.venv/bin/python"}
  [[ -x "$PYTHON" ]] || { echo 'Project Python environment missing. Run ./scripts/setup.sh first.' >&2; exit 2; }
  "$PYTHON" -m unittest discover -s "$ROOT/scripts/tests" -v
  "$PYTHON" -m unittest discover -s "$ROOT/loadtests/tests" -v
  "$PYTHON" -c 'import pytest, psycopg' || { echo 'Install backend test dependencies using scripts/setup.sh.' >&2; exit 2; }
  if [[ -n ${TEST_DATABASE_URL:-} ]]; then
    "$PYTHON" - <<'PY'
import os
from urllib.parse import urlparse
url = urlparse(os.environ['TEST_DATABASE_URL'])
if url.scheme not in {'postgresql+psycopg', 'postgresql'} or not url.path.removeprefix('/').startswith('ticket_test'):
    raise SystemExit('Refusing tests: TEST_DATABASE_URL must refer to a disposable PostgreSQL ticket_test* database.')
PY
  else
    PG_RUNTIME_DIR=$(mktemp -d "${TMPDIR:-/tmp}/ticket-platform-pg16.XXXXXX")
    export PG_RUNTIME_DIR
    if [[ -z ${PG_TEST_PORT:-} ]]; then
      # Another PostgreSQL (for example a load stack) may already hold 55432.
      PG_TEST_PORT=$("$PYTHON" - <<'PY'
import socket
for port in range(55432, 55500):
    with socket.socket() as probe:
        if probe.connect_ex(('127.0.0.1', port)) != 0:
            print(port)
            break
else:
    raise SystemExit('No free port in 55432-55499 for the test PostgreSQL; set PG_TEST_PORT.')
PY
)
      export PG_TEST_PORT
    fi
    CLEANUPS+=('"$ROOT/scripts/pg16.sh" stop >/dev/null 2>&1; echo "PostgreSQL test logs retained at $PG_RUNTIME_DIR" >&2')
    "$ROOT/scripts/pg16.sh" start
    TEST_DATABASE_URL=$("$ROOT/scripts/pg16.sh" url)
    export TEST_DATABASE_URL
  fi
  export DATABASE_URL="$TEST_DATABASE_URL"
  if [[ -z ${REDIS_URL:-} ]]; then
    if docker info >/dev/null 2>&1; then
      REDIS_CONTAINER="ticket-test-redis-$$"
      docker run -d --rm --name "$REDIS_CONTAINER" -p 127.0.0.1::6379 redis:7-alpine \
        redis-server --appendonly yes --appendfsync always >/dev/null
      CLEANUPS+=('docker stop "$REDIS_CONTAINER" >/dev/null 2>&1')
      REDIS_PORT=$(docker port "$REDIS_CONTAINER" 6379/tcp | head -1 | sed 's/.*://')
      for _ in $(seq 1 30); do
        docker exec "$REDIS_CONTAINER" redis-cli ping 2>/dev/null | grep -q PONG && break
        sleep 0.5
      done
      export REDIS_URL="redis://127.0.0.1:$REDIS_PORT/0"
      echo "Disposable Redis 7 ready on 127.0.0.1:$REDIS_PORT ($REDIS_CONTAINER)."
    else
      echo 'REDIS_URL is not set and Docker is unavailable: Redis-dependent tests will be skipped.' >&2
    fi
  fi
  cd "$ROOT/backend"
  "$PYTHON" -m alembic upgrade head
  "$PYTHON" -m pytest "$ROOT/backend/tests" -q -rs
fi

if [[ "$MODE" != backend ]]; then
  cd "$ROOT/frontend"
  [[ -d node_modules ]] || { echo 'Frontend dependencies missing. Run ./scripts/setup.sh first.' >&2; exit 2; }
  npm test -- --run
  npm run build
fi
