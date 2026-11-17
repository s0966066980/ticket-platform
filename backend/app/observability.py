"""Independent observability pipeline: durable telemetry handoff, idempotent
PostgreSQL collection, per-second metrics and configured alerts.

Worker entry points (synchronous; each owns its database sessions). The worker
loop (ticket 09) calls them in this order once per poll, or ``run_pipeline_once``:

    collect_telemetry()   Redis Streams batch -> PostgreSQL commit -> XACK, spool
                          replay, Nginx access.jsonl ingestion (durable checkpoint).
    refresh_metrics()     Rebuild per-second, per-source ``system_metrics`` buckets
                          from persisted, de-duplicated access logs (idempotent upsert).
    evaluate_alerts()     Apply the configured rules with the minimum-request and
                          sustain gates; open/resolve alert rows and keep history.

Process-local producers usable from API, worker and feed code:

    record_service_log(service, code, *, request_id=None, level='INFO', context=None, ...)
    connection_delta(delta)   WebSocket feed reports +1 / -1 per connection.
    emit_connection_gauge()   Heartbeat gauge; the installed app emits it every second.

Telemetry event protocol (version 1, one JSON object per event)::

    {"protocol": 1, "kind": "access" | "service" | "gauge", "event_id": "<uuid>",
     "instance_id": "<process id>", "observed_at": "<ISO-8601 with offset>",
     "clock_error_us": <int>, "clock_valid": <bool>, ...kind specific fields}

The same event_id replayed any number of times persists exactly one row. API
timestamps come from a monotonic clock anchored to the database clock, not the
replica's OS wall clock; events that could not be anchored are placed at
collection time and flagged (large clock_error_us), never silently misplaced.
A Redis acknowledgement never implies the PostgreSQL row exists: the collector
commits first and acknowledges afterwards.

CLI: ``python -m app.observability collect | refresh | alerts | once | ingest --input events.jsonl``.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
from typing import NamedTuple
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from redis import Redis
from redis.exceptions import RedisError, ResponseError
from sqlalchemy import func, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.database import db_now, new_session
from app.models import AccessLog, Alert, OutboxEvent, ServiceLog, SystemMetric

INSTANCE_ID = str(uuid4())
PROTOCOL_VERSION = 1
STREAM = 'ticket:telemetry:v1'
GROUP = 'postgres-v1'
API_SOURCE_PREFIX = 'api:'
GAUGE_SOURCE_PREFIX = 'gauge:'
EVENT_KINDS = frozenset({'access', 'service', 'gauge'})
SKIPPED_PATHS = frozenset({'/health', '/ready', '/docs', '/redoc', '/openapi.json'})
UNANCHORED_CLOCK_ERROR_US = 60_000_000
CLOCK_ERROR_LIMIT_US = 1_000_000      # a sample above this makes its bucket incomplete
ANCHOR_MAX_AGE_SECONDS = 30.0         # unrefreshed anchors older than this are not trusted
GAUGE_STALE_SECONDS = 3               # heartbeat carry-forward before a source counts as missing
RULE_ERROR_RATE = 'error_rate'
RULE_P95_LATENCY = 'p95_latency'
RULE_OUTBOX_LAG = 'outbox_lag'
GLOBAL_SCOPE = 'global'
LOGGER = logging.getLogger('ticket.telemetry')
_SECRET_KEY = re.compile(r'password|token|secret|authorization|cookie|signature|credential', re.I)
_BEARER_VALUE = re.compile(r'bearer\s+\S+', re.I)
_TRACE_ID = re.compile(r'^[A-Za-z0-9_.:-]{1,128}$')


# --- shared helpers ---------------------------------------------------------

def redact(value, depth: int = 0):
    """Bound and scrub a context object so persisted logs never carry secrets."""
    if depth > 5:
        return '[TRUNCATED]'
    if isinstance(value, dict):
        return {
            str(key)[:100]: '[REDACTED]' if _SECRET_KEY.search(str(key)) else redact(item, depth + 1)
            for key, item in list(value.items())[:100]
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, depth + 1) for item in value[:100]]
    if isinstance(value, str):
        return _BEARER_VALUE.sub('Bearer [REDACTED]', value[:2048])
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return redact(str(value), depth)


def _trace(value=None) -> str:
    """A valid request_id: the given one, the current request's, or a fresh UUID."""
    if not value:
        main = sys.modules.get('app.main')  # never import the app factory from a worker
        value = main.REQUEST_ID.get() if main is not None else None
    return value if isinstance(value, str) and _TRACE_ID.fullmatch(value) else str(uuid4())


def _wall_clock() -> datetime:
    return datetime.now(timezone.utc)


def _atomic_json(path: Path, data) -> None:
    """Write-then-rename with fsync so a reader never sees a partial record."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix='.pending-')
    try:
        with os.fdopen(descriptor, 'w') as output:
            json.dump(data, output, separators=(',', ':'))
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(name)


def percentile(sorted_samples: list, quantile: float):
    """Nearest-rank percentile of an ascending list; None when there are no samples."""
    if not sorted_samples:
        return None
    rank = max(1, math.ceil(quantile * len(sorted_samples)))
    return sorted_samples[rank - 1]


def _ms(microseconds) -> float | None:
    return None if microseconds is None else round(microseconds / 1000, 3)


# --- process-local runtime: DB-anchored clock and durable handoff --------------

class _ClockAnchor(NamedTuple):
    db_time: datetime
    monotonic: float
    error_us: int


class _Runtime:
    """One per process: Redis client, spool directory, clock anchor and gauges."""

    def __init__(self, redis_url: str, spool_dir: str):
        self.client = Redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=0.2, socket_timeout=0.5)
        self.spool = Path(spool_dir) / f'spool-{INSTANCE_ID}'
        self.lock = threading.Lock()
        self.anchor: _ClockAnchor | None = None
        self.probe_in_flight = False
        self.probe_done = threading.Event()
        self.next_probe = 0.0
        self.redis_durable = False
        self.redis_check_at = 0.0
        self.connections = 0
        self.lost_events = 0
        self.handoff_max_us = 0
        self.last_gauge = 0.0

    # Clock: the request path only reads the anchor; calibration runs in a
    # background thread so an unreachable database never delays a response.
    def clock(self) -> tuple[datetime, int, bool]:
        current = time.monotonic()
        anchor = self.anchor
        if (anchor is None or current - anchor.monotonic >= 1.0) and current >= self.next_probe:
            self.start_probe()
        if anchor is None:
            return _wall_clock(), UNANCHORED_CLOCK_ERROR_US, False
        age = max(0.0, current - anchor.monotonic)
        # Half the probe round trip plus a 100 ppm drift allowance for the age.
        return anchor.db_time + timedelta(seconds=age), anchor.error_us + int(age * 100), age < ANCHOR_MAX_AGE_SECONDS

    def start_probe(self) -> None:
        with self.lock:
            if self.probe_in_flight:
                return
            self.probe_in_flight = True
            self.probe_done.clear()
            self.next_probe = time.monotonic() + 1.0
        threading.Thread(target=self._probe, name='telemetry-clock', daemon=True).start()

    def _probe(self) -> None:
        try:
            started = time.monotonic()
            with new_session() as session:
                database_time = db_now(session)
            stopped = time.monotonic()
            self.anchor = _ClockAnchor(database_time, (started + stopped) / 2, int((stopped - started) * 500_000))
        except Exception:
            LOGGER.warning('telemetry clock calibration failed; events stay unanchored until the database answers')
            with self.lock:
                self.next_probe = time.monotonic() + 5.0
        finally:
            with self.lock:
                self.probe_in_flight = False
            self.probe_done.set()

    def calibrate(self, timeout: float) -> bool:
        """Start a probe and wait at most ``timeout`` seconds for it to finish."""
        self.start_probe()
        self.probe_done.wait(timeout)
        return self.anchor is not None

    # Handoff: durable Redis stream when AOF fsync-always is confirmed, else a
    # per-replica spool. Failure of both is counted and reported, never hidden.
    def _redis_is_durable(self) -> bool:
        configured = self.client.config_get('append*')
        return configured.get('appendonly') == 'yes' and configured.get('appendfsync') == 'always'

    def emit(self, event: dict) -> bool:
        started = time.monotonic()
        delivered = False
        try:
            if started >= self.redis_check_at:
                self.redis_check_at = started + 5.0
                self.redis_durable = self._redis_is_durable()
            if self.redis_durable:
                self.client.xadd(STREAM, {'event': json.dumps(event, separators=(',', ':'))})
                delivered = True
        except (RedisError, OSError):
            self.redis_durable = False  # back off; the durability probe runs again after 5 s
        if not delivered:
            try:
                _atomic_json(self.spool / f"record-{event['event_id']}.json", event)
                delivered = True
            except OSError:
                with self.lock:
                    self.lost_events += 1
                LOGGER.error('TELEMETRY_PERSISTENCE_FAILED kind=%s request_id=%s', event.get('kind'), event.get('request_id', ''))
        with self.lock:
            self.handoff_max_us = max(self.handoff_max_us, int((time.monotonic() - started) * 1_000_000))
        return delivered


@lru_cache(maxsize=8)
def _runtime(redis_url: str, spool_dir: str) -> _Runtime:
    return _Runtime(redis_url, spool_dir)


def _get_runtime() -> _Runtime:
    settings = get_settings()
    return _runtime(settings.redis_url, str(settings.telemetry_spool_dir))


def _event(kind: str, **fields) -> dict:
    observed_at, error_us, valid = _get_runtime().clock()
    return {
        'protocol': PROTOCOL_VERSION, 'kind': kind, 'event_id': str(uuid4()), 'instance_id': INSTANCE_ID,
        'observed_at': observed_at.isoformat(), 'clock_error_us': error_us, 'clock_valid': valid, **fields,
    }


# --- producers ----------------------------------------------------------------

def record_service_log(service: str, code: str, *, request_id: str | None = None, level: str = 'INFO',
                       context: dict | None = None, span_id: str | None = None, parent_span_id: str | None = None) -> str:
    """Hand off one service log event (API, worker, gateway, callback) and return its event_id."""
    event = _event(
        'service', request_id=_trace(request_id), service=service[:64], code=code[:100], level=level[:16],
        context=redact(context or {}), span_id=span_id, parent_span_id=parent_span_id,
    )
    _get_runtime().emit(event)
    return event['event_id']


def connection_delta(delta: int) -> int:
    """Adjust this replica's live connection gauge; emits when due. Returns the new count."""
    runtime = _get_runtime()
    with runtime.lock:
        runtime.connections = max(0, runtime.connections + delta)
        count = runtime.connections
        due = time.monotonic() - runtime.last_gauge >= 1.0 or count == 0
    if due:
        emit_connection_gauge()
    return count


def emit_connection_gauge() -> str:
    """Emit this replica's heartbeat: connections, lost events and handoff latency."""
    runtime = _get_runtime()
    with runtime.lock:
        count = runtime.connections
        lost = runtime.lost_events
        handoff = runtime.handoff_max_us
        runtime.last_gauge = time.monotonic()
        runtime.handoff_max_us = 0
    event = _event('gauge', request_id='heartbeat', connections=count, lost_events=lost, handoff_us=handoff)
    runtime.emit(event)
    return event['event_id']


def _emit_access(fields: dict) -> None:
    _get_runtime().emit(_event('access', **fields))


# --- ASGI middleware and app installation --------------------------------------

class TelemetryMiddleware:
    """Pure ASGI access logging: monotonic duration, route template, 4xx business
    outcome versus 5xx / unhandled-exception system error. Never fails a request."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope.get('path') in SKIPPED_PATHS:
            await self.app(scope, receive, send)
            return
        started = time.monotonic()
        state = {'status': 500, 'json': False, 'body': bytearray(), 'recorded': False, 'request_id': None}

        async def record(unhandled: bool = False) -> None:
            if state['recorded']:
                return
            state['recorded'] = True
            try:
                await run_in_threadpool(_emit_access, _access_fields(scope, state, started, unhandled))
            except Exception:
                LOGGER.exception('telemetry access handoff failed')

        async def observed_send(message) -> None:
            if message['type'] == 'http.response.start':
                state['status'] = message['status']
                for name, value in message.get('headers', []):
                    if name.lower() == b'content-type' and b'json' in value.lower():
                        state['json'] = True
                    elif name.lower() == b'x-request-id':
                        state['request_id'] = value.decode('latin-1', 'replace')
            elif message['type'] == 'http.response.body':
                if state['json'] and state['status'] >= 400 and len(state['body']) < 8192:
                    state['body'].extend(message.get('body', b'')[:8192 - len(state['body'])])
                await send(message)
                if not message.get('more_body', False):
                    await record()
                return
            await send(message)

        try:
            await self.app(scope, receive, observed_send)
        except Exception:
            await record(unhandled=True)
            raise


def _route_template(scope) -> str:
    """Full route template (prefix included) so metrics never key on raw IDs.

    The matched route only knows its router-relative template; the included
    prefix is recovered from the request path by removing the formatted tail.
    """
    route = scope.get('route')
    template = getattr(route, 'path_format', None) or getattr(route, 'path', None)
    if not template:
        return 'UNMATCHED'
    path = str(scope.get('path', ''))
    convertors = getattr(route, 'param_convertors', {}) or {}
    try:
        tail = template.format(**{
            name: convertors[name].to_string(value) if name in convertors else str(value)
            for name, value in (scope.get('path_params') or {}).items()
        })
    except (KeyError, ValueError, IndexError, TypeError):
        return template
    if path.endswith(tail):
        return path[:len(path) - len(tail)] + template
    return template


def _access_fields(scope, state, started: float, unhandled: bool) -> dict:
    status = state['status']
    code = None
    if 400 <= status < 500 and state['json'] and state['body']:
        with suppress(ValueError, UnicodeError, AttributeError, TypeError):
            code = json.loads(bytes(state['body'])).get('code')
    request_id = _trace(scope.get('state', {}).get('request_id') or state['request_id'])
    route = _route_template(scope)
    if unhandled:
        error_kind = 'UNHANDLED_EXCEPTION'
    elif status >= 500:
        error_kind = 'HTTP_5XX'
    else:
        error_kind = None
    return {
        'request_id': request_id, 'method': str(scope.get('method', 'GET'))[:16], 'route_template': str(route)[:500],
        'status_code': status, 'error_kind': error_kind,
        'business_outcome': str(code or f'HTTP_{status}')[:100] if 400 <= status < 500 else None,
        'duration_us': max(0, int((time.monotonic() - started) * 1_000_000)),
    }


async def _heartbeat_loop() -> None:
    while True:
        await asyncio.sleep(1.0)
        try:
            await asyncio.to_thread(emit_connection_gauge)
        except Exception:
            LOGGER.exception('telemetry heartbeat failed')


def install_observability(app) -> None:
    """Attach access logging and the per-second connection heartbeat to the app.

    The lifespan wrapper calibrates the clock anchor (bounded wait) and starts
    the heartbeat; /health and /ready stay untouched and the app keeps working
    without Redis or a database (events spool, timestamps are flagged).
    """
    app.add_middleware(TelemetryMiddleware)
    inner_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        await asyncio.to_thread(_get_runtime().calibrate, 0.5)
        heartbeat = asyncio.create_task(_heartbeat_loop())
        try:
            async with inner_lifespan(application) as state:
                yield state
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    app.router.lifespan_context = lifespan


# --- collection: protocol validation and idempotent persistence ---------------

def _normalize(event: dict, now: datetime) -> dict:
    if not isinstance(event, dict) or event.get('protocol') != PROTOCOL_VERSION or event.get('kind') not in EVENT_KINDS:
        raise ValueError('Unknown telemetry protocol or kind')
    normalized = dict(event)
    normalized['event_id'] = UUID(str(event['event_id']))
    observed_at = datetime.fromisoformat(str(event['observed_at']))
    if observed_at.tzinfo is None:
        raise ValueError('Telemetry time must include a timezone offset')
    normalized['clock_error_us'] = max(0, int(event.get('clock_error_us', 0)))
    if not event.get('clock_valid', False):
        # The replica could not anchor to the database clock: place the event at
        # collection time and make the uncertainty visible instead of trusting it.
        observed_at = now
        normalized['clock_error_us'] = max(normalized['clock_error_us'], UNANCHORED_CLOCK_ERROR_US)
    normalized['observed_at'] = observed_at
    normalized['request_id'] = _trace(event.get('request_id'))
    normalized['instance_id'] = str(event.get('instance_id') or 'unknown')[:200]
    if normalized['kind'] == 'access':
        normalized['duration_us'] = max(0, int(event['duration_us']))
        normalized['status_code'] = int(event['status_code'])
        normalized['method'] = str(event.get('method', 'GET'))[:16]
        normalized['route_template'] = str(event.get('route_template') or 'UNMATCHED')[:500]
        normalized['error_kind'] = str(event['error_kind'])[:100] if event.get('error_kind') else None
        normalized['business_outcome'] = str(event['business_outcome'])[:100] if event.get('business_outcome') else None
    elif normalized['kind'] == 'service':
        normalized['service'] = str(event.get('service') or 'unknown')[:64]
        normalized['level'] = str(event.get('level') or 'INFO')[:16]
        normalized['code'] = str(event.get('code') or 'UNSPECIFIED')[:100]
        normalized['context'] = redact(event.get('context') or {})
        normalized['span_id'] = str(event['span_id'])[:200] if event.get('span_id') else None
        normalized['parent_span_id'] = str(event['parent_span_id'])[:200] if event.get('parent_span_id') else None
    else:
        normalized['connections'] = max(0, int(event.get('connections', 0)))
        normalized['lost_events'] = max(0, int(event.get('lost_events', 0)))
        normalized['handoff_us'] = max(0, int(event.get('handoff_us', 0)))
    return normalized


def _invalid_record(original, now: datetime) -> dict:
    digest = hashlib.sha256(json.dumps(original, sort_keys=True, default=str).encode()).hexdigest()
    return {
        'event_id': uuid5(NAMESPACE_URL, 'invalid-telemetry:' + digest), 'request_id': 'telemetry-invalid',
        'service': 'collector', 'level': 'ERROR', 'code': 'TELEMETRY_INVALID_RECORD',
        'context': {'sha256': digest}, 'span_id': None, 'parent_span_id': None, 'observed_at': now,
    }


ACCESS_COLUMNS = ('event_id', 'request_id', 'instance_id', 'method', 'route_template', 'status_code',
                  'error_kind', 'business_outcome', 'duration_us', 'observed_at', 'clock_error_us')
SERVICE_COLUMNS = ('event_id', 'request_id', 'service', 'level', 'code', 'context', 'span_id', 'parent_span_id', 'observed_at')


def ingest_events(events, session_factory=new_session) -> int:
    """Persist one batch in one transaction; the caller may acknowledge only after return.

    Replays are harmless: rows are keyed by event_id and conflicts are skipped;
    gauge buckets are overwritten only by a newer observation.
    """
    with session_factory() as session:
        now = db_now(session)
        access, service = [], []
        for original in events:
            try:
                event = _normalize(original, now)
            except (ValueError, KeyError, TypeError, OverflowError):
                service.append(_invalid_record(original, now))
                continue
            if event['kind'] == 'access':
                access.append({column: event[column] for column in ACCESS_COLUMNS})
            elif event['kind'] == 'service':
                service.append({column: event[column] for column in SERVICE_COLUMNS})
            else:
                _upsert_gauge(session, event)
        if access:
            session.execute(insert(AccessLog).values(access).on_conflict_do_nothing(index_elements=['event_id']))
        if service:
            session.execute(insert(ServiceLog).values(service).on_conflict_do_nothing(index_elements=['event_id']))
        session.commit()
    return len(events)


def _upsert_gauge(session: Session, event: dict) -> None:
    statement = insert(SystemMetric).values(
        source_id=GAUGE_SOURCE_PREFIX + event['instance_id'], bucket_at=event['observed_at'].replace(microsecond=0),
        window_seconds=1, request_count=0, error_count=0, business_counts={},
        latency_summary={'handoff_us': event['handoff_us'], 'lost_events': event['lost_events'], 'clock_error_us': event['clock_error_us']},
        connections=event['connections'], last_seen_at=event['observed_at'],
        data_complete=bool(event.get('clock_valid')) and event['lost_events'] == 0,
    )
    session.execute(statement.on_conflict_do_update(
        index_elements=['source_id', 'bucket_at', 'window_seconds'],
        set_={column: getattr(statement.excluded, column) for column in ('connections', 'last_seen_at', 'data_complete', 'latency_summary')},
        where=SystemMetric.last_seen_at <= statement.excluded.last_seen_at,
    ))


def _redis_batch(limit: int) -> list:
    client = _get_runtime().client
    try:
        client.xgroup_create(STREAM, GROUP, id='0-0', mkstream=True)
    except ResponseError as exc:
        if 'BUSYGROUP' not in str(exc):
            raise
    # Re-deliver entries a crashed collector left pending before reading new ones.
    claimed = client.xautoclaim(STREAM, GROUP, INSTANCE_ID, min_idle_time=10_000, start_id='0-0', count=limit)
    messages = list(claimed[1]) if claimed else []
    if len(messages) < limit:
        for _, records in client.xreadgroup(GROUP, INSTANCE_ID, {STREAM: '>'}, count=limit - len(messages), block=1):
            messages.extend(records)
    return messages


def _collect_stream(session_factory, limit: int) -> int:
    messages = _redis_batch(limit)
    if not messages:
        return 0
    events = []
    for identity, fields in messages:
        try:
            events.append(json.loads(fields['event']))
        except (ValueError, KeyError, TypeError):
            events.append({'invalid_stream_entry': identity})
    ingest_events(events, session_factory)  # PostgreSQL commit MUST precede the acknowledgement.
    identities = [identity for identity, _ in messages]
    client = _get_runtime().client
    client.xack(STREAM, GROUP, *identities)
    client.xdel(STREAM, *identities)
    return len(messages)


def _collect_spool(session_factory, limit: int) -> int:
    root = get_settings().telemetry_spool_dir
    if not root.exists():
        return 0
    files, events = [], []
    for path in sorted(root.glob('spool-*/record-*.json')):
        if len(files) >= limit:
            break
        if path.is_symlink():
            continue
        try:
            if path.stat().st_size > 1_000_000:
                events.append({'oversized_spool_record': path.name})
            else:
                events.append(json.loads(path.read_text()))
            files.append(path)
        except FileNotFoundError:
            continue
        except (ValueError, UnicodeError):
            events.append({'invalid_spool_record': path.name})
            files.append(path)
    if not files:
        return 0
    ingest_events(events, session_factory)  # commit before the spool files disappear
    for path in files:
        with suppress(FileNotFoundError):
            path.unlink()
    return len(files)


def ingest_nginx_logs(session_factory=new_session, limit: int = 1000) -> int:
    """Ingest Nginx JSON trace rows as ``service_logs`` (service "nginx").

    They are request traces of the proxy, not API metrics, so they never enter
    access_logs. The byte offset checkpoint is fsynced after the database commit;
    a rotated file (new device/inode) restarts from zero and the event_id derived
    from (file identity, offset, content hash) keeps replays idempotent.
    """
    source = Path(os.environ.get('NGINX_LOG_PATH', '/data/proxy/access.jsonl'))
    if not source.exists():
        return 0
    checkpoint = get_settings().telemetry_spool_dir / 'nginx-checkpoint.json'
    previous = {}
    with suppress(OSError, ValueError):
        previous = json.loads(checkpoint.read_text())
    stat = source.stat()
    identity = {'device': stat.st_dev, 'inode': stat.st_ino}
    same_file = all(previous.get(key) == value for key, value in identity.items())
    offset = previous.get('offset', 0) if same_file and previous.get('offset', 0) <= stat.st_size else 0
    events = []
    with source.open('rb') as lines:
        lines.seek(offset)
        for _ in range(limit):
            position = lines.tell()
            line = lines.readline(1_000_001)
            if not line or not line.endswith(b'\n'):
                break  # a partial last line is read once Nginx finishes writing it
            try:
                data = json.loads(line)
                if not isinstance(data, dict):
                    raise ValueError('Nginx trace row must be an object')
            except (ValueError, UnicodeError):
                data = {'request_id': 'proxy-invalid', 'parse_error': True}
            observed_at = data.get('observed_at') or _wall_clock().isoformat()
            events.append({
                'protocol': PROTOCOL_VERSION, 'kind': 'service',
                'event_id': str(uuid5(NAMESPACE_URL, f"nginx:{stat.st_dev}:{stat.st_ino}:{position}:{hashlib.sha256(line).hexdigest()}")),
                'request_id': data.get('request_id'), 'instance_id': 'nginx', 'service': 'nginx', 'level': 'INFO',
                'code': 'HTTP_PROXY_REQUEST',
                'context': {key: value for key, value in data.items() if key not in {'request_id', 'observed_at', 'service'}},
                'observed_at': observed_at, 'clock_valid': True, 'clock_error_us': 0,
            })
            offset = lines.tell()
    if events:
        ingest_events(events, session_factory)
        _atomic_json(checkpoint, {**identity, 'offset': offset})
    return len(events)


def collect_telemetry(session_factory=new_session, limit: int = 1000) -> dict:
    """One collection pass: Redis stream batch, spool replay, Nginx log ingest.

    Returns counts per source and whether Redis answered. Database failures
    propagate so nothing is acknowledged or deleted without a commit.
    """
    if not 1 <= limit <= 10_000:
        raise ValueError('Telemetry batch limit must be 1..10000')
    counts = {'stream': 0, 'spool': 0, 'nginx': 0, 'redis_available': True}
    try:
        counts['stream'] = _collect_stream(session_factory, limit)
    except (RedisError, OSError):
        counts['redis_available'] = False
    counts['spool'] = _collect_spool(session_factory, limit)
    counts['nginx'] = ingest_nginx_logs(session_factory, limit)
    return counts


# --- per-second metrics -------------------------------------------------------

def refresh_metrics(session_factory=new_session, *, now: datetime | None = None, since: datetime | None = None) -> datetime:
    """Rebuild ``api:<instance>`` one-second buckets touched by recently ingested rows.

    Every bucket is recomputed from all of its persisted rows and overwritten
    (never accumulated), so collector restarts and replays cannot double count.
    ``since`` bounds the ingested_at scan (default: window + 60 s before ``now``);
    a worker passes its previous ``now`` to make the scan incremental. Returns ``now``.
    """
    settings = get_settings()
    with session_factory() as session:
        now = now or db_now(session)
        since = since or now - timedelta(seconds=settings.metrics_window_seconds + 60)
        bucket = func.date_trunc('second', AccessLog.observed_at)
        touched = session.execute(select(AccessLog.instance_id, bucket).where(AccessLog.ingested_at >= since).distinct()).all()
        if not touched:
            return now
        rows = session.execute(select(
            AccessLog.instance_id, bucket.label('bucket_at'), AccessLog.error_kind, AccessLog.business_outcome,
            AccessLog.duration_us, AccessLog.clock_error_us, AccessLog.observed_at,
        ).where(tuple_(AccessLog.instance_id, bucket).in_([tuple(item) for item in touched]))).all()
        groups = defaultdict(list)
        for row in rows:
            groups[(row.instance_id, row.bucket_at)].append(row)
        for (instance_id, bucket_at), items in groups.items():
            samples = sorted(item.duration_us for item in items)
            max_clock_error = max(item.clock_error_us for item in items)
            statement = insert(SystemMetric).values(
                source_id=API_SOURCE_PREFIX + instance_id, bucket_at=bucket_at, window_seconds=1,
                request_count=len(items), error_count=sum(1 for item in items if item.error_kind),
                business_counts=dict(Counter(item.business_outcome for item in items if item.business_outcome)),
                latency_summary={
                    'count': len(samples), 'sum_us': sum(samples), 'max_us': samples[-1],
                    'samples_us': samples, 'max_clock_error_us': max_clock_error,
                },
                connections=0, last_seen_at=max(item.observed_at for item in items),
                data_complete=max_clock_error <= CLOCK_ERROR_LIMIT_US,
            )
            session.execute(statement.on_conflict_do_update(
                index_elements=['source_id', 'bucket_at', 'window_seconds'],
                set_={column: getattr(statement.excluded, column) for column in (
                    'request_count', 'error_count', 'business_counts', 'latency_summary', 'last_seen_at', 'data_complete')},
            ))
        session.commit()
    return now


def window_metrics(session: Session, *, window_seconds: int | None = None, now: datetime | None = None) -> dict:
    """Aggregate all sources over the last ``window_seconds`` one-second buckets.

    Percentiles are computed from the merged latency samples of every API source,
    never from per-replica percentiles. A second with no persisted observation
    (collector lag, missing source) reports null values and data_complete=false
    rather than zero. Connections sum the live heartbeat gauges.
    """
    settings = get_settings()
    window = window_seconds or settings.metrics_window_seconds
    now = now or db_now(session)
    end = now.replace(microsecond=0)
    start = end - timedelta(seconds=window - 1)
    rows = session.scalars(select(SystemMetric).where(
        SystemMetric.window_seconds == 1,
        SystemMetric.bucket_at >= start - timedelta(seconds=GAUGE_STALE_SECONDS),
        SystemMetric.bucket_at <= end,
    )).all()
    gauges, api = defaultdict(dict), defaultdict(dict)
    for row in rows:
        target = gauges if row.source_id.startswith(GAUGE_SOURCE_PREFIX) else api
        target[row.source_id][row.bucket_at] = row
    collected_until = max((row.last_seen_at for row in rows), default=None)
    items, all_samples = [], []
    complete, total_requests, total_errors = True, 0, 0
    for index in range(window):
        second = start + timedelta(seconds=index)
        api_rows = [buckets[second] for buckets in api.values() if second in buckets]
        live, item_complete = [], True
        for buckets in gauges.values():
            recent = [at for at in buckets if at <= second and (second - at).total_seconds() <= GAUGE_STALE_SECONDS]
            if recent:
                latest = buckets[max(recent)]
                live.append(latest.connections)
                item_complete = item_complete and latest.data_complete
            elif any(at < second for at in buckets) and any(at > second for at in buckets):
                item_complete = False  # heartbeat gap inside this source's lifetime
        covered = (api_rows or live) and collected_until is not None and second <= collected_until
        if not covered:
            complete = False
            items.append({'bucket_at': second.isoformat(), 'rps': None, 'p50_ms': None, 'p95_ms': None, 'p99_ms': None,
                          'error_rate': None, 'connections': None, 'error_count': None, 'business_rejections': None,
                          'data_complete': False})
            continue
        samples = sorted(sample for row in api_rows for sample in row.latency_summary.get('samples_us', []))
        requests = sum(row.request_count for row in api_rows)
        errors = sum(row.error_count for row in api_rows)
        rejections = sum(sum(row.business_counts.values()) for row in api_rows)
        item_complete = item_complete and all(row.data_complete for row in api_rows) and bool(live)
        complete = complete and item_complete
        total_requests += requests
        total_errors += errors
        all_samples.extend(samples)
        items.append({
            'bucket_at': second.isoformat(), 'rps': requests,
            'p50_ms': _ms(percentile(samples, 0.50)), 'p95_ms': _ms(percentile(samples, 0.95)), 'p99_ms': _ms(percentile(samples, 0.99)),
            'error_rate': round(errors / requests, 6) if requests else None,
            'connections': sum(live) if live else None,
            'error_count': errors, 'business_rejections': rejections, 'data_complete': item_complete,
        })
    all_samples.sort()
    return {
        'items': items, 'data_complete': complete, 'window_seconds': window,
        'collected_until': collected_until.isoformat() if collected_until else None,
        'request_count': total_requests, 'error_count': total_errors,
        'error_rate': round(total_errors / total_requests, 6) if total_requests else None,
        'p95_ms': _ms(percentile(all_samples, 0.95)),
    }


# --- alerts -------------------------------------------------------------------

def alert_json(alert: Alert) -> dict:
    return {
        'id': str(alert.id), 'rule_id': alert.rule_id, 'scope': alert.scope, 'status': alert.status,
        'first_seen_at': alert.first_seen_at.isoformat(), 'triggered_at': alert.triggered_at.isoformat(),
        'last_seen_at': alert.last_seen_at.isoformat(),
        'resolved_at': alert.resolved_at.isoformat() if alert.resolved_at else None, 'details': alert.details,
    }


def _rule_conditions(session: Session, now: datetime) -> dict[str, tuple[bool, dict]]:
    settings = get_settings()
    metrics = window_metrics(session, now=now)
    enough = metrics['request_count'] >= settings.alert_min_requests
    common = {'window_seconds': metrics['window_seconds'], 'request_count': metrics['request_count'],
              'min_requests': settings.alert_min_requests, 'data_complete': metrics['data_complete']}
    oldest = session.scalar(select(func.min(OutboxEvent.created_at)).where(OutboxEvent.published_at.is_(None)))
    lag = round((now - oldest).total_seconds(), 3) if oldest is not None else None
    return {
        RULE_ERROR_RATE: (
            enough and metrics['error_rate'] is not None and metrics['error_rate'] > settings.alert_error_rate,
            {**common, 'value': metrics['error_rate'], 'threshold': settings.alert_error_rate, 'error_count': metrics['error_count']},
        ),
        RULE_P95_LATENCY: (
            enough and metrics['p95_ms'] is not None and metrics['p95_ms'] > settings.alert_p95_seconds * 1000,
            {**common, 'value_ms': metrics['p95_ms'], 'threshold_ms': settings.alert_p95_seconds * 1000},
        ),
        RULE_OUTBOX_LAG: (
            lag is not None and lag > settings.alert_outbox_lag_seconds,
            {'value_seconds': lag, 'threshold_seconds': settings.alert_outbox_lag_seconds,
             'oldest_unpublished_at': oldest.isoformat() if oldest else None},
        ),
    }


def evaluate_alerts(session_factory=new_session, *, now: datetime | None = None) -> list[dict]:
    """Evaluate the configured rules once and return the open and just-resolved alerts.

    A condition opens a PENDING row on first sight; it becomes FIRING once it has
    held continuously for ALERT_SUSTAIN_SECONDS (triggered_at), and RESOLVED with
    resolved_at once it clears. Pending rows that clear before firing are dropped;
    fired rows are kept as history. One open row per (rule_id, scope).
    """
    settings = get_settings()
    with session_factory() as session:
        now = now or db_now(session)
        conditions = _rule_conditions(session, now)
        open_alerts = {(alert.rule_id, alert.scope): alert for alert in session.scalars(
            select(Alert).where(Alert.resolved_at.is_(None)).with_for_update())}
        touched = []
        for rule_id, (active, details) in conditions.items():
            current = open_alerts.get((rule_id, GLOBAL_SCOPE))
            if active:
                if current is None:
                    current = Alert(rule_id=rule_id, scope=GLOBAL_SCOPE, status='PENDING', first_seen_at=now,
                                    triggered_at=now, last_seen_at=now, details=details)
                    session.add(current)
                else:
                    current.last_seen_at = now
                    current.details = details
                    if current.status == 'PENDING' and (now - current.first_seen_at).total_seconds() >= settings.alert_sustain_seconds:
                        current.status = 'FIRING'
                        current.triggered_at = now
                touched.append(current)
            elif current is not None:
                if current.status == 'PENDING':
                    session.delete(current)
                    continue
                current.status = 'RESOLVED'
                current.resolved_at = now
                touched.append(current)
        session.flush()
        result = [alert_json(alert) for alert in touched]
        session.commit()
    return result


# --- worker convenience and CLI ---------------------------------------------------

def run_pipeline_once(session_factory=new_session, *, limit: int = 1000) -> dict:
    """collect_telemetry -> refresh_metrics -> evaluate_alerts, for one worker poll."""
    collected = collect_telemetry(session_factory, limit)
    refreshed_at = refresh_metrics(session_factory)
    alerts = evaluate_alerts(session_factory, now=refreshed_at)
    return {'collected': collected, 'refreshed_at': refreshed_at.isoformat(), 'alerts': alerts}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='python -m app.observability', description=__doc__.split('\n\n')[0])
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('collect', help='one Redis/spool/Nginx collection pass')
    commands.add_parser('refresh', help='rebuild recently touched per-second metric buckets')
    commands.add_parser('alerts', help='evaluate the configured alert rules once')
    commands.add_parser('once', help='collect, refresh and evaluate alerts once')
    ingest = commands.add_parser('ingest', help='replay a JSON-lines file of protocol events into PostgreSQL')
    ingest.add_argument('--input', required=True)
    args = parser.parse_args(argv)
    if args.command == 'ingest':
        with open(args.input, 'rb') as lines:
            events = [json.loads(line) for line in lines if line.strip()]
        output = {'ingested': ingest_events(events)}
    elif args.command == 'collect':
        output = collect_telemetry()
    elif args.command == 'refresh':
        output = {'refreshed_at': refresh_metrics().isoformat()}
    elif args.command == 'alerts':
        output = {'alerts': evaluate_alerts()}
    else:
        output = run_pipeline_once()
    print(json.dumps(output, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
