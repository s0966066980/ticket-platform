"""Worker cycle at the CLI/service seam: expiry, publication and failure isolation."""
from datetime import datetime, timedelta, timezone
import json
import os
import subprocess
import sys
from uuid import UUID, uuid4

import pytest
from redis import Redis

pytestmark = pytest.mark.integration


@pytest.fixture(scope='module', autouse=True)
def redis_url():
    url = os.environ.get('REDIS_URL')
    if not url:
        pytest.skip('REDIS_URL not set: real Redis 7 worker execution unavailable')
    Redis.from_url(url, socket_connect_timeout=2).ping()
    return url


def _order(client, db_session):
    from app.models import Seat
    credentials = {'email': 'worker-buyer@example.test', 'password': 'Worker-test-123!'}
    assert client.post('/api/v1/auth/register', json=credentials).status_code == 201
    headers = {'Authorization': 'Bearer ' + client.post('/api/v1/auth/login', json=credentials).json()['token']}
    now = datetime.now(timezone.utc)
    event = client.post('/api/v1/events', headers=headers, json={'title': 'W', 'description': '', 'category': 'music', 'venue': 'Taipei', 'sales_start': (now - timedelta(hours=1)).isoformat(), 'sales_end': (now + timedelta(days=1)).isoformat(), 'zones': [{'code': 'A', 'name': 'A', 'price_twd': 100}]}).json()
    seat = Seat(event_id=UUID(event['id']), zone_id=UUID(event['zones'][0]['id']), row_label='1', seat_number='1')
    db_session.add(seat)
    db_session.commit()
    assert client.post('/api/v1/events/' + event['id'] + '/publish', headers={**headers, 'If-Match': str(event['version'])}).status_code == 200
    order = client.post('/api/v1/events/' + event['id'] + '/bookings', headers={**headers, 'Idempotency-Key': str(uuid4())}, json={'seat_ids': [str(seat.id)]})
    assert order.status_code == 201
    return headers, event, order.json(), seat.id


def test_one_worker_cycle_expires_due_holds_and_publishes_their_events(client, db_session):
    from sqlalchemy import text
    from app.models import EventStream, OutboxEvent, Seat
    from app.worker import run_once
    headers, event, order, seat_id = _order(client, db_session)
    db_session.execute(text("UPDATE orders SET expires_at = clock_timestamp() - interval '1 second' WHERE id = :id"), {'id': order['id']})
    db_session.execute(text("UPDATE seats SET held_until = clock_timestamp() - interval '1 second' WHERE id = :id"), {'id': seat_id})
    db_session.commit()
    summary = run_once()
    assert summary['expire_due_orders'] == 1
    assert client.get('/api/v1/orders/' + order['id'], headers=headers).json()['status'] == 'EXPIRED'
    db_session.expire_all()
    assert db_session.get(Seat, seat_id).status == 'AVAILABLE'
    unpublished = db_session.query(OutboxEvent).filter(OutboxEvent.kind == 'seat_changed', OutboxEvent.published_at.is_(None)).count()
    assert unpublished == 0 and db_session.get(EventStream, UUID(event['id'])).published_version == 2
    assert run_once()['expire_due_orders'] == 0, 'a second cycle finds nothing to do'


def test_failing_job_is_isolated_and_reported(client, db_session, monkeypatch):
    from app import worker
    def broken():
        raise RuntimeError('boom')
    jobs = worker._jobs()
    jobs['orders'].append(worker.Job('broken', broken, 1.0))
    monkeypatch.setattr(worker, '_jobs', lambda: jobs)
    summary = worker.run_once()
    assert summary['broken'] == {'error': 'RuntimeError'}
    assert 'publish_outbox' in summary, 'other jobs still ran'


def test_cli_once_prints_json_summary(test_database_url, redis_url):
    env = {**os.environ, 'DATABASE_URL': test_database_url, 'REDIS_URL': redis_url}
    result = subprocess.run([sys.executable, '-m', 'app.worker', '--once'], capture_output=True, text=True, env=env, timeout=120)
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout.strip().splitlines()[-1])
    assert {'expire_due_orders', 'publish_outbox', 'process_import_jobs'} <= set(summary)
    assert subprocess.run([sys.executable, '-m', 'app.worker', '--bogus'], capture_output=True, env=env).returncode == 2
