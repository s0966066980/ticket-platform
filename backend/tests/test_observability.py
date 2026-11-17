"""Observability checks at the HTTP, collector-CLI and PostgreSQL seams (A16, A17, A19)."""
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

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
    INSTANCE_ID, collect_telemetry, emit_connection_gauge, evaluate_alerts, ingest_events,
    record_service_log, refresh_metrics,
)
from tests.test_auth import bearer, login, seed_test_accounts

pytestmark = pytest.mark.integration
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


def seed_access_logs(session, instance_id, observed_at, durations_ms, *, status=200, error_kind=None, business_outcome=None, clock_error_us=0):
    rows = [AccessLog(
        request_id=f'seed-{uuid4().hex[:12]}', instance_id=instance_id, method='GET', route_template='/api/v1/events',
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
    url = os.environ.get('REDIS_URL_FOR_TESTS', 'redis://127.0.0.1:6379/0')
    if not redis_available(url):
        pytest.skip(f'Redis 7 not reachable at {url}; stream path unverified in this run')
    monkeypatch.setenv('REDIS_URL', url)
    get_settings.cache_clear()
    client = Redis.from_url(url, decode_responses=True)
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
