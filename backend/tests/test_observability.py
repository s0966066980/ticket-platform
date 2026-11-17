"""Observability checks at the HTTP, collector-CLI and PostgreSQL seams (A16, A17, A19)."""
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import pytest
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import observability
from app.config import get_settings
from app.database import db_now, get_engine, get_session
from app.main import create_app
from app.models import AccessLog, Alert, OutboxEvent, ServiceLog, SystemMetric
from app.observability import (
    INSTANCE_ID, collect_telemetry, connection_delta, emit_connection_gauge, evaluate_alerts, ingest_events,
    record_service_log, refresh_metrics,
)
from tests.test_auth import bearer, login, seed_test_accounts

pytestmark = pytest.mark.integration
# Captured at import: fixtures point REDIS_URL at an unreachable port to exercise the spool path.
REAL_REDIS_URL = os.environ.get('REDIS_URL_FOR_TESTS') or os.environ.get('REDIS_URL') or 'redis://127.0.0.1:6379/0'
BACKEND = Path(__file__).parents[1]
UNREACHABLE_REDIS = 'redis://127.0.0.1:1/0'


@pytest.fixture
def telemetry_env(tmp_path, monkeypatch):
    """Isolated spool directory and no Redis unless a test opts in."""
    spool = tmp_path / 'telemetry'
    monkeypatch.setenv('TELEMETRY_SPOOL_DIR', str(spool))
    monkeypatch.setenv('REDIS_URL', UNREACHABLE_REDIS)
    monkeypatch.setenv('NGINX_LOG_PATH', str(tmp_path / 'missing-access.jsonl'))
    get_settings.cache_clear()
    yield spool
    get_settings.cache_clear()


@pytest.fixture
def observed_client(telemetry_env, db_session):
    """The real app with the observability hook plus one deliberately failing route."""
    app = create_app()

    def boom():
        raise RuntimeError('simulated unhandled failure')

    app.add_api_route('/api/v1/boom', boom, methods=['GET'])

    def override_session():
        with Session(get_engine(), expire_on_commit=False) as request_session:
            yield request_session

    app.dependency_overrides[get_session] = override_session
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def admin_token(observed_client):
    seed_test_accounts()
    return login(observed_client, 'admin@example.test', 'Admin-test-123!').json()['token']


def run_cli(*arguments):
    return subprocess.run([sys.executable, '-m', 'app.observability', *arguments], cwd=BACKEND, env=os.environ.copy(), text=True, capture_output=True, check=False)


def seed_access_logs(session, instance_id, observed_at, durations_ms, *, status=200, error_kind=None, business_outcome=None, clock_error_us=0, method='GET', route='/api/v1/events'):
    rows = [AccessLog(
        request_id=f'seed-{uuid4().hex[:12]}', instance_id=instance_id, method=method, route_template=route,
        status_code=status, error_kind=error_kind, business_outcome=business_outcome, duration_us=int(duration * 1000),
        observed_at=observed_at, clock_error_us=clock_error_us,
    ) for duration in durations_ms]
    session.add_all(rows)
    session.commit()
    return rows


def bucket_items(payload):
    return {item['bucket_at']: item for item in payload['items']}


def second(moment):
    return moment.replace(microsecond=0)


# --- authorization ---------------------------------------------------------------

@pytest.mark.parametrize('route', ['/api/v1/admin/metrics', '/api/v1/admin/alerts', '/api/v1/admin/logs/probe-1'])
def test_admin_observation_routes_require_authentication(observed_client, route):
    response = observed_client.get(route)
    assert response.status_code == 401
    assert response.json()['code'] == 'UNAUTHENTICATED'


@pytest.mark.parametrize('email,password,expected', [
    ('admin@example.test', 'Admin-test-123!', 200),
    ('user@example.test', 'User-test-123!', 403),
    ('viewer@example.test', 'Viewer-test-123!', 403),
])
@pytest.mark.parametrize('route', ['/api/v1/admin/metrics', '/api/v1/admin/alerts', '/api/v1/admin/logs/probe-1'])
def test_only_admin_can_read_metrics_alerts_and_request_logs(observed_client, route, email, password, expected):
    seed_test_accounts()
    token = login(observed_client, email, password).json()['token']
    response = observed_client.get(route, headers=bearer(token))
    assert response.status_code == expected
    if expected == 200:
        body = response.json()
        assert isinstance(body['items'], list)
        if route.endswith('/metrics'):
            assert len(body['items']) == get_settings().metrics_window_seconds
        else:
            assert body['next_cursor'] is None


# --- durable handoff and request logs ------------------------------------------------

def test_request_is_spooled_without_redis_and_persisted_by_the_collector(observed_client, admin_token, db_session, telemetry_env):
    response = observed_client.get('/api/v1/admin/users', headers={**bearer(admin_token), 'X-Request-ID': 'trace-spool-1'})
    assert response.status_code == 200
    spooled = list(telemetry_env.glob('spool-*/record-*.json'))
    assert spooled, 'without Redis the access event must land in the spool, never be dropped'

    counts = collect_telemetry()
    assert counts['redis_available'] is False
    assert counts['spool'] >= 1
    assert not list(telemetry_env.glob('spool-*/record-*.json'))

    row = db_session.scalar(select(AccessLog).where(AccessLog.request_id == 'trace-spool-1'))
    assert row is not None
    assert (row.method, row.route_template, row.status_code) == ('GET', '/api/v1/admin/users', 200)
    assert row.instance_id == INSTANCE_ID
    assert row.error_kind is None and row.business_outcome is None
    assert row.duration_us >= 0
    assert row.clock_error_us <= observability.CLOCK_ERROR_LIMIT_US, 'the running app must anchor to the database clock'
    assert abs((row.observed_at - db_now(db_session)).total_seconds()) < 30


def test_admin_logs_combine_services_by_request_id_and_never_expose_secrets(observed_client, admin_token, db_session):
    assert observed_client.get('/api/v1/admin/users', headers={**bearer(admin_token), 'X-Request-ID': 'trace-multi-1'}).status_code == 200
    record_service_log('worker', 'PAYMENT_CALLBACK_ACCEPTED', request_id='trace-multi-1', context={
        'order_id': 'order-1', 'signature': 'raw-signature-value', 'password': 'hunter2', 'nested': {'token': 'tok-123'},
        'note': f'Authorization: Bearer {admin_token}',
    })
    record_service_log('gateway', 'CHARGE_CREATED', request_id='trace-multi-1', level='INFO', context={'amount_twd': 1200})
    collect_telemetry()

    response = observed_client.get('/api/v1/admin/logs/trace-multi-1', headers=bearer(admin_token))
    assert response.status_code == 200
    body = response.json()
    assert body['next_cursor'] is None
    assert {item['service'] for item in body['items']} == {'api', 'worker', 'gateway'}
    assert [item['observed_at'] for item in body['items']] == sorted(item['observed_at'] for item in body['items'])
    api_item = next(item for item in body['items'] if item['service'] == 'api')
    assert api_item['code'] == 'HTTP_200' and api_item['level'] == 'INFO'
    assert api_item['context']['route_template'] == '/api/v1/admin/users'
    worker_item = next(item for item in body['items'] if item['service'] == 'worker')
    assert worker_item['context']['order_id'] == 'order-1'
    assert worker_item['context']['signature'] == '[REDACTED]'
    assert worker_item['context']['nested']['token'] == '[REDACTED]'
    text = response.text
    for secret in ('raw-signature-value', 'hunter2', 'tok-123', admin_token):
        assert secret not in text
    for row in db_session.scalars(select(ServiceLog)):
        assert admin_token not in json.dumps(row.context) and 'hunter2' not in json.dumps(row.context)

    paged = observed_client.get('/api/v1/admin/logs/trace-multi-1?limit=2', headers=bearer(admin_token)).json()
    assert len(paged['items']) == 2 and paged['next_cursor']
    rest = observed_client.get(f"/api/v1/admin/logs/trace-multi-1?limit=2&cursor={paged['next_cursor']}", headers=bearer(admin_token)).json()
    assert len(rest['items']) == 1 and rest['next_cursor'] is None
    assert {item['event_id'] for item in paged['items'] + rest['items']} == {item['event_id'] for item in body['items']}


def test_replaying_the_same_event_id_persists_exactly_one_row(telemetry_env, db_session, tmp_path):
    now = db_now(db_session)
    event = {
        'protocol': 1, 'kind': 'access', 'event_id': str(uuid4()), 'instance_id': 'replica-a', 'request_id': 'trace-replay',
        'observed_at': now.isoformat(), 'clock_error_us': 10, 'clock_valid': True, 'method': 'POST',
        'route_template': '/api/v1/events/{event_id}/bookings', 'status_code': 409, 'error_kind': None,
        'business_outcome': 'SEAT_UNAVAILABLE', 'duration_us': 4200,
    }
    replay = tmp_path / 'events.jsonl'
    replay.write_text(json.dumps(event) + '\n' + json.dumps(event) + '\n')
    for _ in range(2):
        completed = run_cli('ingest', '--input', str(replay))
        assert completed.returncode == 0, completed.stderr
        assert json.loads(completed.stdout) == {'ingested': 2}
    assert ingest_events([event]) == 1
    rows = list(db_session.scalars(select(AccessLog).where(AccessLog.request_id == 'trace-replay')))
    assert len(rows) == 1
    assert rows[0].business_outcome == 'SEAT_UNAVAILABLE' and rows[0].error_kind is None


def test_malformed_telemetry_is_recorded_as_a_collector_error_not_dropped(telemetry_env, db_session):
    assert ingest_events([{'protocol': 99, 'kind': 'access'}, 'garbage']) == 2
    invalid = list(db_session.scalars(select(ServiceLog).where(ServiceLog.code == 'TELEMETRY_INVALID_RECORD')))
    assert len(invalid) == 2 and all(row.service == 'collector' and row.level == 'ERROR' for row in invalid)


def redis_available(url):
    try:
        return Redis.from_url(url, socket_connect_timeout=0.5).ping()
    except (RedisError, OSError):
        return False


def test_stream_path_commits_to_postgres_before_acknowledging(telemetry_env, db_session, monkeypatch):
    url = REAL_REDIS_URL
    if not redis_available(url):
        pytest.skip(f'Redis 7 not reachable at {url}; stream path unverified in this run')
    monkeypatch.setenv('REDIS_URL', url)
    get_settings.cache_clear()
    client = Redis.from_url(url, decode_responses=True)
    # The handoff only trusts a Redis whose AOF is fsynced on every write (as the
    # compose service is configured). CI's service container starts without it,
    # so enable it for this test and restore the previous settings afterwards.
    previous = {key: client.config_get(key)[key] for key in ('appendonly', 'appendfsync')}
    try:
        client.config_set('appendonly', 'yes')
        client.config_set('appendfsync', 'always')
    except RedisError as exc:
        pytest.skip(f'cannot enable AOF on {url}: {exc}')
    client.delete(observability.STREAM)
    app = create_app()
    app.dependency_overrides[get_session] = lambda: (yield from (session for session in [db_session]))
    with TestClient(app) as http:
        assert http.get('/api/v1/auth/me', headers={'X-Request-ID': 'trace-stream-1'}).status_code == 401
    assert client.xlen(observability.STREAM) >= 1, 'with AOF fsync-always confirmed the event goes to the stream'
    assert not list(telemetry_env.glob('spool-*/record-*.json'))

    counts = collect_telemetry()
    assert counts['redis_available'] is True and counts['stream'] >= 1
    assert client.xlen(observability.STREAM) == 0
    assert client.xpending(observability.STREAM, observability.GROUP)['pending'] == 0
    row = db_session.scalar(select(AccessLog).where(AccessLog.request_id == 'trace-stream-1'))
    assert row is not None and row.status_code == 401 and row.business_outcome == 'UNAUTHENTICATED'
    assert collect_telemetry()['stream'] == 0
    client.delete(observability.STREAM)
    for key, value in previous.items():
        client.config_set(key, value)


# --- per-second metrics -------------------------------------------------------------

def test_metrics_merge_samples_across_instances_and_do_not_report_gaps_as_zero(observed_client, admin_token, db_session):
    base = second(db_now(db_session)) - timedelta(seconds=20)
    # Two replicas, one second: merged samples 1..100 ms must give exact p50/p95/p99.
    seed_access_logs(db_session, 'replica-a', base + timedelta(milliseconds=100), range(1, 51))
    seed_access_logs(db_session, 'replica-b', base + timedelta(milliseconds=900), range(51, 101))
    # Next second: 4 of 10 requests are 5xx, plus 3 business rejections that are not errors.
    seed_access_logs(db_session, 'replica-a', base + timedelta(seconds=1), [10] * 6)
    seed_access_logs(db_session, 'replica-a', base + timedelta(seconds=1), [10] * 4, status=500, error_kind='HTTP_5XX')
    seed_access_logs(db_session, 'replica-b', base + timedelta(seconds=1), [10] * 3, status=409, business_outcome='SEAT_UNAVAILABLE')
    refresh_metrics()

    stored = {row.source_id: row for row in db_session.scalars(select(SystemMetric).where(SystemMetric.bucket_at == base))}
    assert stored['api:replica-a'].request_count == 50 and stored['api:replica-b'].request_count == 50
    assert stored['api:replica-a'].latency_summary['samples_us'][:3] == [1000, 2000, 3000]
    # Refreshing again overwrites instead of accumulating.
    refresh_metrics()
    db_session.expire_all()
    assert db_session.get(SystemMetric, ('api:replica-a', base, 1)).request_count == 50

    body = observed_client.get('/api/v1/admin/metrics', headers=bearer(admin_token)).json()
    assert len(body['items']) == get_settings().metrics_window_seconds
    items = bucket_items(body)
    merged = items[base.isoformat()]
    assert merged['rps'] == 100
    assert (merged['p50_ms'], merged['p95_ms'], merged['p99_ms']) == (50.0, 95.0, 99.0)
    assert merged['error_rate'] == 0.0
    mixed = items[(base + timedelta(seconds=1)).isoformat()]
    assert mixed['rps'] == 13 and mixed['error_count'] == 4 and mixed['business_rejections'] == 3
    assert mixed['error_rate'] == pytest.approx(4 / 13, abs=1e-6)
    # Seconds nobody observed are reported as missing, not as zero traffic.
    empty = items[(base + timedelta(seconds=5)).isoformat()]
    assert empty['rps'] is None and empty['p95_ms'] is None and empty['error_rate'] is None and empty['connections'] is None
    assert empty['data_complete'] is False
    assert body['data_complete'] is False
    # Seeded replicas never sent a heartbeat: the connection gauge is unknown, not 0.
    assert merged['connections'] is None and merged['data_complete'] is False


def test_unanchored_replica_clock_is_flagged_not_silently_misplaced(telemetry_env, db_session):
    now = db_now(db_session)
    slow_by_20s = now - timedelta(seconds=20)
    anchored = {
        'protocol': 1, 'kind': 'access', 'event_id': str(uuid4()), 'instance_id': 'replica-ok', 'request_id': 'trace-ok',
        'observed_at': slow_by_20s.isoformat(), 'clock_error_us': 400, 'clock_valid': True, 'method': 'GET',
        'route_template': '/api/v1/events', 'status_code': 200, 'duration_us': 1000,
    }
    unanchored = {**anchored, 'event_id': str(uuid4()), 'instance_id': 'replica-slow', 'request_id': 'trace-slow', 'clock_valid': False}
    ingest_events([anchored, unanchored])
    ok = db_session.scalar(select(AccessLog).where(AccessLog.request_id == 'trace-ok'))
    slow = db_session.scalar(select(AccessLog).where(AccessLog.request_id == 'trace-slow'))
    assert ok.observed_at == slow_by_20s and ok.clock_error_us == 400
    assert abs((slow.observed_at - now).total_seconds()) < 5, 'placed at collection time, not 20 s in the past'
    assert slow.clock_error_us >= observability.UNANCHORED_CLOCK_ERROR_US

    refresh_metrics()
    buckets = {row.source_id: row for row in db_session.scalars(select(SystemMetric).where(SystemMetric.source_id.like('api:%')))}
    assert buckets['api:replica-ok'].bucket_at == second(slow_by_20s) and buckets['api:replica-ok'].data_complete is True
    assert buckets['api:replica-slow'].data_complete is False


def test_live_app_requests_classify_business_rejections_separately_from_system_errors(observed_client, admin_token, db_session):
    assert connection_delta(2) == 2  # two WebSocket clients attached on this replica; emits a gauge
    assert observed_client.get('/api/v1/admin/users').status_code == 401                       # business: UNAUTHENTICATED
    assert observed_client.get('/api/v1/does-not-exist', headers=bearer(admin_token)).status_code == 404
    assert observed_client.get('/api/v1/boom', headers=bearer(admin_token)).status_code == 500  # unhandled exception
    assert observed_client.get('/api/v1/admin/users', headers=bearer(admin_token)).status_code == 200
    emit_connection_gauge()
    collect_telemetry()
    refresh_metrics()

    rows = {row.route_template: row for row in db_session.scalars(select(AccessLog))}
    assert rows['/api/v1/admin/users'].status_code in (200, 401)
    rejected = db_session.scalar(select(AccessLog).where(AccessLog.status_code == 401))
    assert (rejected.business_outcome, rejected.error_kind) == ('UNAUTHENTICATED', None)
    assert (rows['UNMATCHED'].status_code, rows['UNMATCHED'].business_outcome) == (404, 'NOT_FOUND')
    assert (rows['/api/v1/boom'].status_code, rows['/api/v1/boom'].error_kind, rows['/api/v1/boom'].business_outcome) == (500, 'UNHANDLED_EXCEPTION', None)
    assert all(row.clock_error_us <= observability.CLOCK_ERROR_LIMIT_US for row in rows.values())

    body = observed_client.get('/api/v1/admin/metrics', headers=bearer(admin_token)).json()
    observed = [item for item in body['items'] if item['rps']]
    assert sum(item['rps'] for item in observed) >= 5
    assert sum(item['error_count'] for item in observed) == 1
    assert sum(item['business_rejections'] for item in observed) == 2
    assert all(item['connections'] == 2 and item['data_complete'] for item in observed), 'heartbeats make these seconds complete'
    assert connection_delta(-2) == 0


# --- alerts ---------------------------------------------------------------------------

def open_alerts(session):
    return list(session.scalars(select(Alert).order_by(Alert.first_seen_at)))


def test_error_rate_alert_needs_min_requests_and_sustain_then_resolves_keeping_history(observed_client, admin_token, db_session):
    t0 = second(db_now(db_session))
    sustain = timedelta(seconds=get_settings().alert_sustain_seconds)
    seed_access_logs(db_session, 'replica-a', t0 - timedelta(seconds=2), [5] * 10, status=500, error_kind='HTTP_5XX')
    refresh_metrics(now=t0)
    assert evaluate_alerts(now=t0) == []
    assert open_alerts(db_session) == [], '100% errors over 10 requests is below the 20-request gate'

    seed_access_logs(db_session, 'replica-b', t0 - timedelta(seconds=1), [5] * 15, status=503, error_kind='HTTP_5XX')
    refresh_metrics(now=t0)
    evaluate_alerts(now=t0)
    evaluate_alerts(now=t0 + sustain - timedelta(seconds=1))
    pending = open_alerts(db_session)
    assert [alert.status for alert in pending] == ['PENDING']
    assert observed_client.get('/api/v1/admin/alerts', headers=bearer(admin_token)).json()['items'] == []

    fired = evaluate_alerts(now=t0 + sustain + timedelta(seconds=1))
    assert [(alert['rule_id'], alert['status']) for alert in fired] == [('error_rate', 'FIRING')]
    assert fired[0]['details']['request_count'] == 25 and fired[0]['details']['value'] == 1.0
    listed = observed_client.get('/api/v1/admin/alerts', headers=bearer(admin_token)).json()['items']
    assert [(item['rule_id'], item['status']) for item in listed] == [('error_rate', 'FIRING')]
    assert listed[0]['triggered_at'] == (t0 + sustain + timedelta(seconds=1)).isoformat()
    assert listed[0]['resolved_at'] is None

    # The window slides past the failures: the condition clears and history is kept.
    later = t0 + timedelta(seconds=get_settings().metrics_window_seconds + 30)
    resolved = evaluate_alerts(now=later)
    assert [(alert['rule_id'], alert['status']) for alert in resolved] == [('error_rate', 'RESOLVED')]
    assert evaluate_alerts(now=later + timedelta(seconds=1)) == []
    listed = observed_client.get('/api/v1/admin/alerts', headers=bearer(admin_token)).json()['items']
    assert [(item['status'], item['resolved_at']) for item in listed] == [('RESOLVED', later.isoformat())]

    # A new episode opens a second row instead of reviving the old one.
    seed_access_logs(db_session, 'replica-a', later + timedelta(seconds=5), [5] * 25, status=500, error_kind='HTTP_5XX')
    refresh_metrics(now=later + timedelta(seconds=6))
    evaluate_alerts(now=later + timedelta(seconds=6))
    evaluate_alerts(now=later + timedelta(seconds=6) + sustain)
    listed = observed_client.get('/api/v1/admin/alerts', headers=bearer(admin_token)).json()['items']
    assert [item['status'] for item in listed] == ['FIRING', 'RESOLVED']
    assert len({item['id'] for item in listed}) == 2


def test_p95_latency_alert_uses_merged_samples_and_outbox_lag_alert_tracks_oldest_unpublished(observed_client, admin_token, db_session):
    t0 = second(db_now(db_session))
    sustain = timedelta(seconds=get_settings().alert_sustain_seconds)
    # Each replica alone has p95 = 900 ms; merged, 4 of 20 samples exceed 1 s so p95 is 1500 ms.
    seed_access_logs(db_session, 'replica-a', t0 - timedelta(seconds=3), [900] * 8 + [1500] * 2)
    seed_access_logs(db_session, 'replica-b', t0 - timedelta(seconds=2), [900] * 8 + [1500] * 2)
    refresh_metrics(now=t0)
    evaluate_alerts(now=t0)
    fired = evaluate_alerts(now=t0 + sustain)
    assert [(alert['rule_id'], alert['status']) for alert in fired] == [('p95_latency', 'FIRING')]
    assert fired[0]['details']['value_ms'] == 1500.0

    db_session.add(OutboxEvent(kind='seat_changed', aggregate_id=uuid4(), aggregate_version=1, payload={}, request_id='trace-outbox', created_at=t0 - timedelta(seconds=30)))
    db_session.commit()
    pending = {alert['rule_id']: alert for alert in evaluate_alerts(now=t0 + sustain + timedelta(seconds=1))}
    assert pending['outbox_lag']['status'] == 'PENDING' and pending['outbox_lag']['details']['value_seconds'] > 10
    fired = {alert['rule_id']: alert for alert in evaluate_alerts(now=t0 + 2 * sustain + timedelta(seconds=1))}
    assert fired['outbox_lag']['status'] == 'FIRING'
    db_session.execute(OutboxEvent.__table__.update().values(published_at=func.clock_timestamp()))
    db_session.commit()
    cleared = {alert['rule_id']: alert for alert in evaluate_alerts(now=t0 + 2 * sustain + timedelta(seconds=2))}
    assert cleared['outbox_lag']['status'] == 'RESOLVED' and cleared['p95_latency']['status'] == 'FIRING'
    listed = observed_client.get('/api/v1/admin/alerts?limit=1', headers=bearer(admin_token)).json()
    assert len(listed['items']) == 1 and listed['next_cursor']
    rest = observed_client.get(f"/api/v1/admin/alerts?limit=1&cursor={listed['next_cursor']}", headers=bearer(admin_token)).json()
    assert {item['rule_id'] for item in listed['items'] + rest['items']} == {'p95_latency', 'outbox_lag'}


# --- Nginx trace ingestion ---------------------------------------------------------------

def test_nginx_trace_rows_become_service_logs_with_durable_checkpoint(observed_client, admin_token, db_session, telemetry_env, tmp_path, monkeypatch):
    log = tmp_path / 'access.jsonl'
    monkeypatch.setenv('NGINX_LOG_PATH', str(log))
    rows = [
        {'service': 'nginx', 'request_id': 'nginx-abc123', 'method': 'GET', 'path': '/api/v1/admin/users', 'status': '200', 'duration_seconds': '0.012', 'observed_at': '2026-01-05T10:00:00+08:00'},
        {'service': 'nginx', 'request_id': 'nginx-def456', 'method': 'POST', 'path': '/api/v1/auth/login', 'status': '401', 'duration_seconds': '0.004', 'observed_at': '2026-01-05T10:00:01+08:00'},
    ]
    log.write_text(''.join(json.dumps(row) + '\n' for row in rows) + '{"request_id": "partial"')
    assert observed_client.get('/api/v1/admin/users', headers={**bearer(admin_token), 'X-Request-ID': 'nginx-abc123'}).status_code == 200

    assert collect_telemetry()['nginx'] == 2
    assert collect_telemetry()['nginx'] == 0, 'the checkpoint prevents re-reading complete lines'
    with log.open('a') as handle:
        handle.write(', "observed_at": "2026-01-05T10:00:02+08:00"}\n')
    completed = run_cli('once')  # the worker-style pass: collect, refresh, evaluate
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)['collected']['nginx'] == 1
    assert db_session.scalar(select(func.count()).select_from(ServiceLog).where(ServiceLog.service == 'nginx')) == 3
    assert db_session.scalar(select(func.count()).select_from(AccessLog).where(AccessLog.request_id == 'nginx-def456')) == 0, 'proxy traces are not API metrics'
    assert db_session.scalar(select(func.count()).select_from(AccessLog).where(AccessLog.request_id == 'nginx-abc123')) == 1

    body = observed_client.get('/api/v1/admin/logs/nginx-abc123', headers=bearer(admin_token)).json()
    assert [item['service'] for item in body['items']] == ['nginx', 'api']
    assert body['items'][0]['context']['status'] == '200' and body['items'][0]['code'] == 'HTTP_PROXY_REQUEST'
    assert datetime.fromisoformat(body['items'][0]['observed_at']) == datetime.fromisoformat('2026-01-05T10:00:00+08:00')


def test_booking_rejection_alert_fires_when_http_looks_healthy(observed_client, admin_token, db_session):
    """Every answer is a well-formed 409: no 5xx, p95 fine, yet nobody can buy."""
    from app.observability import BOOKING_ROUTE
    t0 = second(db_now(db_session))
    sustain = timedelta(seconds=get_settings().alert_sustain_seconds)
    seed_access_logs(db_session, 'replica-a', t0 - timedelta(seconds=2), [20] * 19, status=409, business_outcome='SEAT_UNAVAILABLE', method='POST', route=BOOKING_ROUTE)
    refresh_metrics(now=t0)
    evaluate_alerts(now=t0)
    assert [alert.rule_id for alert in open_alerts(db_session)] == [], '19 bookings is below the 20-request gate'
    seed_access_logs(db_session, 'replica-b', t0 - timedelta(seconds=1), [20] * 2, status=201, method='POST', route=BOOKING_ROUTE)
    seed_access_logs(db_session, 'replica-b', t0 - timedelta(seconds=1), [20] * 4, status=409, business_outcome='QUOTA_EXCEEDED', method='POST', route=BOOKING_ROUTE)
    refresh_metrics(now=t0)
    evaluate_alerts(now=t0)
    fired = {alert['rule_id']: alert for alert in evaluate_alerts(now=t0 + sustain)}
    assert fired['booking_rejection_rate']['status'] == 'FIRING'
    assert fired['booking_rejection_rate']['details'] == {**fired['booking_rejection_rate']['details'], 'booking_requests': 25, 'rejected_409': 23, 'value': 0.92}
    assert 'error_rate' not in fired and 'p95_latency' not in fired, 'the system-health rules stay quiet'
    listed = observed_client.get('/api/v1/admin/alerts', headers=bearer(admin_token)).json()['items']
    assert [(item['rule_id'], item['status']) for item in listed] == [('booking_rejection_rate', 'FIRING')]


def test_payment_failure_alert_counts_settled_mock_payments(observed_client, admin_token, db_session):
    from app.models import Order, Payment, User
    t0 = second(db_now(db_session))
    sustain = timedelta(seconds=get_settings().alert_sustain_seconds)
    user = db_session.scalar(select(User).limit(1))
    credentials = {'email': 'payer-alert@example.test', 'password': 'Payer-alert-123!'}
    observed_client.post('/api/v1/auth/register', json=credentials)
    buyer = db_session.scalar(select(User).where(User.email == credentials['email']))
    from datetime import timezone as _tz
    event = observed_client.post('/api/v1/events', headers=bearer(observed_client.post('/api/v1/auth/login', json=credentials).json()['token']), json={
        'title': 'Alert show', 'description': '', 'category': 'music', 'venue': 'Taipei',
        'sales_start': (t0 - timedelta(hours=1)).isoformat(), 'sales_end': (t0 + timedelta(days=1)).isoformat(),
        'zones': [{'code': 'A', 'name': 'A', 'price_twd': 100}]}).json()
    for index in range(24):
        order = Order(event_id=UUID(event['id']), user_id=buyer.id, status='PENDING_PAYMENT', ticket_count=1, subtotal_twd=100, fee_twd=5, total_twd=105, expires_at=t0 + timedelta(minutes=5))
        db_session.add(order)
        db_session.flush()
        db_session.add(Payment(order_id=order.id, status='FAILED' if index < 20 else 'SUCCEEDED', amount_twd=105, request_id='seed', updated_at=t0 - timedelta(seconds=5)))
    db_session.commit()
    evaluate_alerts(now=t0)
    fired = {alert['rule_id']: alert for alert in evaluate_alerts(now=t0 + sustain)}
    assert fired['payment_failure_rate']['status'] == 'FIRING'
    assert fired['payment_failure_rate']['details']['settled_attempts'] == 24 and fired['payment_failure_rate']['details']['failed'] == 20
    assert fired['payment_failure_rate']['details']['value'] == round(20 / 24, 4)
    later = t0 + timedelta(seconds=get_settings().metrics_window_seconds + 30)
    assert [alert['status'] for alert in evaluate_alerts(now=later) if alert['rule_id'] == 'payment_failure_rate'] == ['RESOLVED']


def test_hourly_purge_removes_old_telemetry_but_keeps_alerts_and_open_claims(observed_client, admin_token, db_session):
    from app.models import IdempotencyKey, ListSnapshot, Session as LoginSession, User
    from app.observability import purge_expired
    t0 = second(db_now(db_session))
    old = t0 - timedelta(days=get_settings().telemetry_retention_days + 1)
    seed_access_logs(db_session, 'replica-a', old, [5] * 3)
    seed_access_logs(db_session, 'replica-a', t0 - timedelta(seconds=2), [5] * 2)
    db_session.add(ServiceLog(request_id='old-trace', service='worker', level='INFO', code='X', context={}, observed_at=old))
    db_session.add(SystemMetric(source_id='api:old', bucket_at=old, window_seconds=1, request_count=1, error_count=0, business_counts={}, latency_summary={}, connections=0, last_seen_at=old, data_complete=True))
    db_session.add(Alert(rule_id='error_rate', scope='global', status='RESOLVED', first_seen_at=old, triggered_at=old, last_seen_at=old, resolved_at=old, details={}))
    user = db_session.scalar(select(User).limit(1))
    login = db_session.scalar(select(LoginSession).where(LoginSession.user_id == user.id).limit(1))
    db_session.add(ListSnapshot(session_id=login.id, user_id=user.id, auth_version=user.auth_version, filter_spec={}, sort_spec={}, page_size=20, total_count=0, expires_at=t0 - timedelta(minutes=1)))
    db_session.add(IdempotencyKey(user_id=user.id, operation='booking:x', key='done', request_hash='a' * 64, state='COMPLETED', generation=1, lease_until=old, expires_at=t0 - timedelta(hours=1), response_status=201, response_body={}))
    db_session.add(IdempotencyKey(user_id=user.id, operation='booking:x', key='stuck', request_hash='b' * 64, state='IN_PROGRESS', generation=1, lease_until=old, expires_at=t0 - timedelta(hours=1)))
    db_session.commit()
    removed = purge_expired(now=t0, batch=2)
    assert removed == {'access_logs': 3, 'service_logs': 1, 'system_metrics': 1, 'list_snapshots': 1, 'idempotency_keys': 1}
    assert db_session.scalar(select(func.count(AccessLog.event_id))) == 2
    assert db_session.scalar(select(func.count(Alert.id))) == 1, 'alert history survives'
    assert [key.key for key in db_session.scalars(select(IdempotencyKey))] == ['stuck'], 'unfinished claims are never purged'
    assert purge_expired(now=t0) == {name: 0 for name in removed}
    assert observed_client.get('/api/v1/admin/alerts', headers=bearer(admin_token)).json()['items'][0]['status'] == 'RESOLVED'
