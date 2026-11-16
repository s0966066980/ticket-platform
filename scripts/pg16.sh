#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
RUNTIME=${PG_RUNTIME_DIR:-"$ROOT/.runtime/pg16"}
PORT=${PG_TEST_PORT:-55432}
DB=${PG_TEST_DATABASE:-ticket_test}
ACTION=${1:-help}

if [[ "$ACTION" == help || "$ACTION" == --help ]]; then
  echo 'Usage: scripts/pg16.sh check|start|stop|status|url'
  echo 'Own isolated PostgreSQL 16 cluster only; never starts or stops a system database.'
  echo 'Settings: PG_BIN, PG_RUNTIME_DIR, PG_TEST_PORT (55432), PG_TEST_DATABASE (ticket_test).'
  exit 0
fi

if [[ -z ${PG_BIN:-} ]]; then
  for candidate in /usr/lib/postgresql/16/bin /usr/local/pgsql16/bin /opt/homebrew/opt/postgresql@16/bin; do
    if [[ -x "$candidate/postgres" ]]; then PG_BIN=$candidate; break; fi
  done
fi
if [[ -z ${PG_BIN:-} || ! -x "$PG_BIN/postgres" || ! -x "$PG_BIN/initdb" || ! -x "$PG_BIN/pg_ctl" ]] ||
   ! "$PG_BIN/postgres" --version | grep -qE 'PostgreSQL\) 16\.'; then
  echo 'PostgreSQL 16 binaries are required; set PG_BIN to their directory.' >&2
  exit 2
fi
if [[ "$ACTION" == check ]]; then "$PG_BIN/postgres" --version; exit 0; fi
[[ "$PORT" =~ ^[0-9]+$ && "$PORT" -ge 1024 && "$PORT" -le 65535 ]] || { echo 'Invalid PG_TEST_PORT.' >&2; exit 2; }
[[ "$DB" =~ ^ticket_test[a-z0-9_]*$ ]] || { echo 'PG_TEST_DATABASE must start with ticket_test and contain only lowercase letters, digits or underscores.' >&2; exit 2; }
[[ "$RUNTIME" =~ ^/[a-zA-Z0-9_./-]+$ && ! -L "$RUNTIME" ]] || { echo 'PG_RUNTIME_DIR must be an absolute non-symlink path using letters, digits, slash, dot, underscore or dash.' >&2; exit 2; }

owned_cluster() {
  [[ -f "$RUNTIME/.ticket-platform-cluster" && -f "$RUNTIME/data/PG_VERSION" ]] &&
    [[ "$(cat "$RUNTIME/.ticket-platform-cluster")" == "$ROOT" ]] &&
    [[ "$(cat "$RUNTIME/data/PG_VERSION")" == 16 ]]
}

case "$ACTION" in
  start)
    [[ $(id -u) != 0 ]] || { echo 'initdb must run as a non-root user.' >&2; exit 2; }
    mkdir -p "$RUNTIME"
    if [[ -d "$RUNTIME/data" ]]; then
      owned_cluster || { echo 'Refusing an unrecognized PostgreSQL directory.' >&2; exit 2; }
    else
      mkdir -p "$RUNTIME/socket"
      "$PG_BIN/initdb" -D "$RUNTIME/data" -U ticket_test -A trust --no-locale -E UTF8 > "$RUNTIME/initdb.log"
      printf '%s\n' "$ROOT" > "$RUNTIME/.ticket-platform-cluster"
    fi
    if ! "$PG_BIN/pg_ctl" -D "$RUNTIME/data" status >/dev/null 2>&1; then
      if ! "$PG_BIN/pg_ctl" -D "$RUNTIME/data" -l "$RUNTIME/postgres.log" -o "-h 127.0.0.1 -p $PORT -k $RUNTIME/socket" -w start; then
        echo "PostgreSQL did not start. See $RUNTIME/postgres.log; socket restrictions must not be bypassed." >&2
        exit 1
      fi
    fi
    running_port=$(sed -n '4p' "$RUNTIME/data/postmaster.pid")
    [[ "$running_port" == "$PORT" ]] || {
      echo "Owned cluster runs on port $running_port, not $PORT; refusing to connect to a different server." >&2
      exit 2
    }
    if ! "$PG_BIN/psql" -X -h 127.0.0.1 -p "$PORT" -U ticket_test -d postgres -Atc 'SELECT datname FROM pg_database' | grep -Fxq "$DB"; then
      "$PG_BIN/createdb" -h 127.0.0.1 -p "$PORT" -U ticket_test "$DB"
    fi
    echo "Isolated PostgreSQL 16 ready on 127.0.0.1:$PORT ($DB)."
    ;;
  stop)
    owned_cluster || { echo 'Refusing to stop an unrecognized PostgreSQL directory.' >&2; exit 2; }
    if "$PG_BIN/pg_ctl" -D "$RUNTIME/data" status >/dev/null 2>&1; then
      "$PG_BIN/pg_ctl" -D "$RUNTIME/data" -m fast -w stop
    fi
    ;;
  status)
    owned_cluster || { echo 'No owned PostgreSQL cluster initialized.' >&2; exit 1; }
    "$PG_BIN/pg_ctl" -D "$RUNTIME/data" status
    ;;
  url) printf 'postgresql+psycopg://ticket_test@127.0.0.1:%s/%s\n' "$PORT" "$DB" ;;
  *) echo "Unknown command: $ACTION" >&2; exit 2 ;;
esac
