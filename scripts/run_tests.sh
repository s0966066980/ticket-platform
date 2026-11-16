#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
MODE=all
case "${1:-}" in
  --help|-h)
    echo 'Usage: scripts/run_tests.sh [--backend-only|--frontend-only]'
    echo 'Runs CLI contracts, backend tests on isolated PostgreSQL 16, frontend tests and build.'
    echo 'Run scripts/setup.sh first. Set TEST_DATABASE_URL only for an explicitly disposable ticket_test* database.'
    exit 0 ;;
  --backend-only) MODE=backend ;;
  --frontend-only) MODE=frontend ;;
  '') ;;
  *) echo "Unknown argument: $1" >&2; exit 2 ;;
esac
[[ $# -le 1 ]] || { echo 'Too many arguments.' >&2; exit 2; }
if [[ "$MODE" != frontend ]]; then
  PYTHON=${PYTHON:-"$ROOT/.venv/bin/python"}
  [[ -x "$PYTHON" ]] || { echo 'Project Python environment missing. Run ./scripts/setup.sh first.' >&2; exit 2; }
  "$PYTHON" -m unittest discover -s "$ROOT/scripts/tests" -v
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
    cleanup() {
      "$ROOT/scripts/pg16.sh" stop >/dev/null 2>&1 || true
      echo "PostgreSQL test logs retained at $PG_RUNTIME_DIR" >&2
    }
    trap cleanup EXIT
    "$ROOT/scripts/pg16.sh" start
    TEST_DATABASE_URL=$("$ROOT/scripts/pg16.sh" url)
    export TEST_DATABASE_URL
  fi
  export DATABASE_URL="$TEST_DATABASE_URL"
  cd "$ROOT/backend"
  "$PYTHON" -m alembic upgrade head
  "$PYTHON" -m pytest "$ROOT/backend/tests" -q
fi

if [[ "$MODE" != backend ]]; then
  cd "$ROOT/frontend"
  [[ -d node_modules ]] || { echo 'Frontend dependencies missing. Run ./scripts/setup.sh first.' >&2; exit 2; }
  npm test -- --run
  npm run build
fi
