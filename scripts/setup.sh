#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
if [[ "${1:-}" == --help ]]; then
  echo 'Usage: PYTHON_BIN=python3.13 ./scripts/setup.sh'
  echo 'Creates .venv, installs backend test dependencies, then installs frontend lockfile dependencies.'
  exit 0
fi
[[ $# -eq 0 ]] || { echo 'Unknown argument; use --help.' >&2; exit 2; }
PYTHON_BIN=${PYTHON_BIN:-python3}
"$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info < (3, 13):
    raise SystemExit('Python 3.13 or newer is required.')
PY
[[ -f "$ROOT/backend/pyproject.toml" && -f "$ROOT/frontend/package-lock.json" ]] || {
  echo 'Backend package metadata or frontend lockfile missing; complete the source checkout first.' >&2; exit 2;
}
if [[ ! -x "$ROOT/.venv/bin/python" ]]; then "$PYTHON_BIN" -m venv "$ROOT/.venv"; fi
"$ROOT/.venv/bin/python" -m pip install -e "$ROOT/backend[test]"
cd "$ROOT/frontend"
npm ci --cache "${NPM_CONFIG_CACHE:-$ROOT/.runtime/npm-cache}"
