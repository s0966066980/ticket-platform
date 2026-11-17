#!/usr/bin/env sh
set -eu
cd /app/backend
case "${1:-api}" in
  api)
    python /usr/local/bin/ticket-prepare-db
    exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers "${API_WORKERS:-4}" --proxy-headers --forwarded-allow-ips='*'
    ;;
  worker) exec python -m app.worker ;;
  gateway) exec python -m uvicorn app.gateway:app --host 0.0.0.0 --port 8001 ;;
  test)
    shift
    python -m alembic upgrade head
    exec python -m pytest tests "$@"
    ;;
  *) exec "$@" ;;
esac
