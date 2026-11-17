"""Background worker: hold expiry, seat imports, Outbox publication, payments and telemetry.

Every job is an idempotent service entry point that another worker may run at
the same time (row locks, leases and generations make duplicates harmless).
Jobs are grouped into threads so a long import never delays seat publication.

CLI: ``python -m app.worker`` (loops until SIGTERM/SIGINT) or
``python -m app.worker --once`` (one cycle, prints a JSON summary).
"""
from dataclasses import dataclass, field
from importlib import import_module, util
import json
import logging
import os
import signal
import socket
import sys
import threading
import time
from typing import Callable

from app.booking import expire_due_orders
from app.config import get_settings
from app.database import new_session
from app.feed import publish_outbox
from app.imports import cleanup_orphan_uploads, process_import_jobs

LOG = logging.getLogger('ticket.worker')
WORKER_ID = f'{socket.gethostname()}:{os.getpid()}'
_MISSING: set[str] = set()


def _optional(module: str, name: str) -> Callable | None:
    """Resolve a service entry point from a module that may not be merged yet; warn once."""
    if util.find_spec(module) is None:
        function = None
    else:
        function = getattr(import_module(module), name, None)
    if function is None and f'{module}.{name}' not in _MISSING:
        _MISSING.add(f'{module}.{name}')
        LOG.warning('Worker job unavailable: %s.%s', module, name)
    return function


def _call_optional(module: str, name: str, *args, **kwargs):
    function = _optional(module, name)
    return function(*args, **kwargs) if function is not None else None


def _record_failure(job: str, error: BaseException) -> None:
    LOG.exception('Worker job %s failed: %s', job, type(error).__name__)
    record = _optional('app.observability', 'record_service_log')
    if record is not None:
        try:
            record('worker', 'WORKER_JOB_FAILED', request_id=f'worker:{job}', level='ERROR', context={'job': job, 'error': type(error).__name__})
        except Exception:
            LOG.debug('Failure log handoff failed for %s', job)


@dataclass
class Job:
    name: str
    run: Callable[[], object]
    interval: float
    next_due: float = field(default=0.0)


def _jobs() -> dict[str, list[Job]]:
    settings = get_settings()
    poll = max(0.05, settings.worker_poll_seconds)
    # Dict order is the run_once order: publication last so a single cycle
    # publishes what the mutations above it committed.
    return {
        'orders': [
            Job('expire_due_orders', lambda: expire_due_orders(), poll),
            Job('process_payment_jobs', lambda: _call_optional('app.payments', 'process_payment_jobs'), poll),
            Job('reconcile_payments', lambda: _call_optional('app.payments', 'reconcile_payments'), max(poll, settings.gateway_poll_seconds)),
        ],
        'imports': [
            Job('process_import_jobs', lambda: process_import_jobs(new_session, WORKER_ID, limit=1), poll),
            Job('cleanup_orphan_uploads', _cleanup_uploads, 3600),
        ],
        'telemetry': [
            Job('collect_telemetry', lambda: _call_optional('app.observability', 'collect_telemetry'), 1.0),
            Job('refresh_metrics', lambda: _call_optional('app.observability', 'refresh_metrics'), 1.0),
            Job('evaluate_alerts', lambda: _call_optional('app.observability', 'evaluate_alerts'), 1.0),
        ],
        'feed': [Job('publish_outbox', lambda: publish_outbox(), max(0.05, settings.outbox_poll_seconds))],
    }


def _cleanup_uploads():
    with new_session() as session:
        return cleanup_orphan_uploads(session)


def run_once() -> dict[str, object]:
    """Execute every job exactly once (tests and the --once CLI); failures are reported, not raised."""
    summary: dict[str, object] = {}
    for jobs in _jobs().values():
        for job in jobs:
            try:
                result = job.run()
                summary[job.name] = result if isinstance(result, (int, dict, type(None))) else str(result)
            except Exception as exc:
                _record_failure(job.name, exc)
                summary[job.name] = {'error': type(exc).__name__}
    return summary


def _group_loop(name: str, jobs: list[Job], stop: threading.Event) -> None:
    while not stop.is_set():
        now = time.monotonic()
        for job in jobs:
            if now < job.next_due:
                continue
            try:
                job.run()
            except Exception as exc:
                _record_failure(job.name, exc)
            job.next_due = time.monotonic() + job.interval
        wait = max(0.01, min(job.next_due for job in jobs) - time.monotonic())
        stop.wait(wait)
    LOG.info('Worker group %s stopped', name)


def serve(stop: threading.Event | None = None) -> None:
    stop = stop or threading.Event()
    threads = [threading.Thread(target=_group_loop, args=(name, jobs, stop), name=f'worker-{name}', daemon=True) for name, jobs in _jobs().items()]
    for thread in threads:
        thread.start()
    LOG.info('Worker %s started groups: %s', WORKER_ID, ', '.join(thread.name for thread in threads))
    try:
        while not stop.is_set():
            stop.wait(1)
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=30)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=os.environ.get('LOG_LEVEL', 'INFO'), format='%(asctime)s %(levelname)s %(name)s %(message)s')
    argv = sys.argv[1:] if argv is None else argv
    if argv == ['--once']:
        print(json.dumps(run_once(), default=str))
        return 0
    if argv:
        print('Usage: python -m app.worker [--once]', file=sys.stderr)
        return 2
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    serve(stop)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
