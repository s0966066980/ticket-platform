#!/usr/bin/env bash
# Browser end-to-end suite against a running stack (docker compose up -d --build).
# Waits for E2E_BASE_URL/health, then runs e2e/ with pytest + Playwright (Chromium headless).
# Exit code is pytest's. Failure evidence lands in e2e/artifacts/.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
case "${1:-}" in
  --help|-h)
    echo 'Usage: E2E_BASE_URL=http://localhost:8080 scripts/run_e2e.sh [pytest args...]'
    echo 'Waits up to E2E_WAIT_SECONDS (default 180) for the stack health endpoint, then runs the e2e suite.'
    exit 0 ;;
esac
export E2E_BASE_URL="${E2E_BASE_URL:-http://localhost:8080}"
PYTHON=${PYTHON:-"$ROOT/.venv/bin/python"}
[[ -x "$PYTHON" ]] || { echo "Python environment missing at $PYTHON. Install e2e/requirements.txt first." >&2; exit 2; }
"$PYTHON" -c 'import pytest, playwright, pytest_playwright, openpyxl' 2>/dev/null \
  || { echo 'E2E dependencies missing: install e2e/requirements.txt and run "python -m playwright install chromium".' >&2; exit 2; }

WAIT=${E2E_WAIT_SECONDS:-180}
HEALTH="${E2E_BASE_URL%/}/health"
echo "Waiting up to ${WAIT}s for ${HEALTH} ..."
for ((i = 0; i < WAIT; i++)); do
  if body=$(curl -fsS --max-time 3 "$HEALTH" 2>/dev/null) && [[ "$body" == *'"status":"ok"'* ]]; then
    echo "Stack healthy: $body"
    cd "$ROOT"
    exec "$PYTHON" -m pytest e2e -q "$@"
  fi
  sleep 1
done
echo "Stack at ${HEALTH} did not become healthy within ${WAIT}s." >&2
exit 1
