"""Seat feed at the public seams: WebSocket protocol, HTTP mutations and the Outbox publisher."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
import threading
from uuid import UUID, uuid4

import pytest
from redis import Redis
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.config import get_settings
from app.feed import CLOSE_NOT_FOUND, CLOSE_PROTOCOL, CLOSE_RESYNC, CLOSE_UNAUTHENTICATED, FeedHub, publish_outbox
from app.main import create_app

pytestmark = pytest.mark.integration


@pytest.fixture(scope='module', autouse=True)
def redis_url():
    url = os.environ.get('REDIS_URL')
    if not url:
        pytest.skip('REDIS_URL not set: real Redis 7 feed execution unavailable')
    client = Redis.from_url(url, socket_connect_timeout=2)
    client.ping()
    yield url
    client.close()


@pytest.fixture
def redis_client(redis_url):
    client = Redis.from_url(redis_url, socket_connect_timeout=2)
    yield client
    client.close()


def account(client, email, password='Feed-test-123!'):
    assert client.post('/api/v1/auth/register', json={'email': email, 'password': password}).status_code == 201
    token = client.post('/api/v1/auth/login', json={'email': email, 'password': password}).json()['token']
    return token, {'Authorization': 'Bearer ' + token}


def viewer(db_session, client, email='viewer-feed@example.test'):
    from app.models import User
    token, headers = account(client, email)
    db_session.query(User).filter(User.email == email).update({'role': 'VIEWER'})
    db_session.commit()
    return token, headers


def published_event(client, db_session, headers, seats=2, publish=True):
    from app.models import Seat
    now = datetime.now(timezone.utc)
    payload = {'title': 'Feed show', 'description': '', 'category': 'music', 'venue': 'Taipei',
               'sales_start': (now - timedelta(hours=1)).isoformat(), 'sales_end': (now + timedelta(days=1)).isoformat(),
               'zones': [{'code': 'A', 'name': 'A', 'price_twd': 100}]}
    event = client.post('/api/v1/events', headers=headers, json=payload).json()
    ids = []
    for number in range(1, seats + 1):
        seat = Seat(event_id=UUID(event['id']), zone_id=UUID(event['zones'][0]['id']), row_label='1', seat_number=str(number))
        db_session.add(seat)
        db_session.commit()
        ids.append(str(seat.id))
    if publish:
        assert client.post('/api/v1/events/' + event['id'] + '/publish', headers={**headers, 'If-Match': str(event['version'])}).status_code == 200
    return event, ids


def receive(ws, timeout=5.0):
    """Receive one frame or fail; never hang the suite on a missing frame."""
    outcome = {}

    def run():
        try:
            outcome['frame'] = ws.receive_json()
        except BaseException as exc:  # WebSocketDisconnect must propagate to the caller.
            outcome['error'] = exc
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise AssertionError(f'No WebSocket frame within {timeout}s')
    if 'error' in outcome:
        raise outcome['error']
    return outcome['frame']


def expect_close(ws, code, timeout=5.0):
    with pytest.raises(WebSocketDisconnect) as info:
        receive(ws, timeout)
    assert info.value.code == code


def subscribe(client, event_id, token):
    ws = client.websocket_connect('/ws/events/' + event_id)
    ws.__enter__()
    ws.send_json({'type': 'auth', 'token': token})
    frame = receive(ws)
    assert frame['type'] == 'auth_ok'
    return ws, frame['version']


def test_feed_rejects_missing_auth_frame_and_invalid_tokens(client, db_session):
    _, headers = account(client, 'owner-feed@example.test')
    event, _ = published_event(client, db_session, headers)
    with client.websocket_connect('/ws/events/' + event['id']) as ws:
        ws.send_json({'type': 'heartbeat'})
        expect_close(ws, CLOSE_PROTOCOL)
    with client.websocket_connect('/ws/events/' + event['id']) as ws:
        ws.send_json({'type': 'auth', 'token': 'not-a-session'})
        assert receive(ws) == {'type': 'error', 'code': 'UNAUTHENTICATED'}
        expect_close(ws, CLOSE_UNAUTHENTICATED)
    assert client.get('/api/v1/events/' + event['id'] + '/version', headers=headers).json() == {'version': 0}


def test_draft_activity_is_invisible_to_viewers_but_open_to_its_owner(client, db_session):
    owner_token, headers = account(client, 'drafter@example.test')
    event, _ = published_event(client, db_session, headers, publish=False)
    viewer_token, _ = viewer(db_session, client)
    with client.websocket_connect('/ws/events/' + event['id']) as ws:
        ws.send_json({'type': 'auth', 'token': viewer_token})
        assert receive(ws)['code'] == 'NOT_FOUND'
        expect_close(ws, CLOSE_NOT_FOUND)
    ws, version = subscribe(client, event['id'], owner_token)
    assert version == 0
    ws.__exit__(None, None, None)


def test_subscriber_receives_committed_changes_in_version_order(client, db_session, redis_client):
    owner_token, headers = account(client, 'buyer-feed@example.test')
    event, seat_ids = published_event(client, db_session, headers)
    viewer_token, _ = viewer(db_session, client)
    ws, version = subscribe(client, event['id'], viewer_token)
    assert version == 0
    ws.send_json({'type': 'heartbeat'})
    assert receive(ws) == {'type': 'heartbeat', 'version': 0}

    order = client.post('/api/v1/events/' + event['id'] + '/bookings', headers={**headers, 'Idempotency-Key': str(uuid4())}, json={'seat_ids': [seat_ids[0]]})
    assert order.status_code == 201
    assert publish_outbox(redis_client) == 1
    frame = receive(ws)
    assert frame['type'] == 'seat_changed' and frame['version'] == 1 and frame['event_id'] == event['id']
    assert [(change['id'], change['status']) for change in frame['changes']] == [(seat_ids[0], 'HELD')]
    assert frame['changes'][0]['held_until'] is not None

    assert client.post('/api/v1/orders/' + order.json()['id'] + '/cancel', headers=headers).status_code == 200
    assert publish_outbox(redis_client) == 1
    frame = receive(ws)
    assert frame['version'] == 2 and frame['changes'][0]['status'] == 'AVAILABLE' and frame['changes'][0]['held_until'] is None

    ws.send_json({'type': 'heartbeat'})
    assert receive(ws) == {'type': 'heartbeat', 'version': 2}
    assert publish_outbox(redis_client) == 0, 'a second run must not republish'
    ws.__exit__(None, None, None)


def test_publisher_marks_rows_published_and_advances_stream_version(client, db_session, redis_client):
    from app.models import EventStream, OutboxEvent
    _, headers = account(client, 'publisher@example.test')
    event, seat_ids = published_event(client, db_session, headers)
    for seat in seat_ids:
        assert client.post('/api/v1/events/' + event['id'] + '/bookings', headers={**headers, 'Idempotency-Key': str(uuid4())}, json={'seat_ids': [seat]}).status_code == 201
    assert publish_outbox(redis_client) == 2
    rows = db_session.query(OutboxEvent).filter(OutboxEvent.aggregate_id == UUID(event['id'])).order_by(OutboxEvent.aggregate_version).all()
    assert [row.aggregate_version for row in rows] == [1, 2] and all(row.published_at is not None for row in rows)
    stream = db_session.get(EventStream, UUID(event['id']))
    assert (stream.current_version, stream.published_version) == (2, 2)


def test_bulk_import_resync_marker_is_forwarded_unchanged(client, db_session, redis_client):
    from app.models import EventStream, OutboxEvent, SeatEvent
    owner_token, headers = account(client, 'importer-feed@example.test')
    event, _ = published_event(client, db_session, headers)
    ws, _ = subscribe(client, event['id'], owner_token)
    stream = db_session.get(EventStream, UUID(event['id']))
    stream.current_version += 1
    payload = {'event_id': event['id'], 'version': stream.current_version, 'changes': [], 'resync_required': True}
    db_session.add(SeatEvent(event_id=stream.event_id, version=stream.current_version, changes=payload, request_id='import'))
    db_session.add(OutboxEvent(kind='seat_changed', aggregate_id=stream.event_id, aggregate_version=stream.current_version, payload=payload, request_id='import'))
    db_session.commit()
    assert publish_outbox(redis_client) == 1
    frame = receive(ws)
    assert frame['resync_required'] is True and frame['version'] == 1 and frame['changes'] == []
    ws.__exit__(None, None, None)


def test_every_api_replica_delivers_the_same_versions(client, db_session, redis_client):
    """Two app instances (two hubs) attached to one Redis both see each committed version."""
    owner_token, headers = account(client, 'replica@example.test')
    event, seat_ids = published_event(client, db_session, headers)
    with TestClient(create_app()) as replica:
        first, _ = subscribe(client, event['id'], owner_token)
        second, _ = subscribe(replica, event['id'], owner_token)
        assert client.post('/api/v1/events/' + event['id'] + '/bookings', headers={**headers, 'Idempotency-Key': str(uuid4())}, json={'zone_id': event['zones'][0]['id'], 'quantity': 2}).status_code == 201
        assert publish_outbox(redis_client) == 1
        frames = [receive(first), receive(second)]
        assert all(frame['version'] == 1 and len(frame['changes']) == 2 for frame in frames)
        first.__exit__(None, None, None)
        second.__exit__(None, None, None)


def test_logged_out_subscriber_is_disconnected_by_periodic_verification(db_session, monkeypatch):
    monkeypatch.setenv('WS_VERSION_CHECK_SECONDS', '0.2')
    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as client:
            token, headers = account(client, 'revoked-feed@example.test')
            event, _ = published_event(client, db_session, headers)
            ws, _ = subscribe(client, event['id'], token)
            assert receive(ws)['type'] == 'heartbeat', 'silent connections receive the DB version periodically'
            assert client.post('/api/v1/auth/logout', headers=headers).status_code in (200, 204)
            frames = []
            with pytest.raises(WebSocketDisconnect) as info:
                for _ in range(20):
                    frames.append(receive(ws))
            assert info.value.code == CLOSE_UNAUTHENTICATED
            assert all(frame['type'] == 'heartbeat' for frame in frames)
            ws.__exit__(None, None, None)
    finally:
        get_settings.cache_clear()


class _StalledSocket:
    """A client that never reads: send_text blocks forever, close records the code."""
    def __init__(self):
        from starlette.websockets import WebSocketState
        self.client_state = WebSocketState.CONNECTED
        self.application_state = WebSocketState.CONNECTED
        self.closed_with = None
        self.sent = []

    async def send_text(self, data):
        self.sent.append(json.loads(data))
        await asyncio.Event().wait()

    async def close(self, code=1000):
        self.closed_with = code


@pytest.mark.asyncio
async def test_slow_subscriber_overflow_closes_with_resync_code(redis_url):
    from app.auth import Principal
    hub = FeedHub(redis_url, queue_size=3, verify_interval=60)
    socket = _StalledSocket()
    principal = Principal(id=uuid4(), email='slow@example.test', role='VIEWER', is_active=True, auth_version=0, session_id=uuid4())
    event_id = uuid4()
    subscription = hub.subscribe(socket, event_id, 'hash', principal)
    pump = asyncio.create_task(subscription.pump())
    subscription.task = pump
    await asyncio.sleep(0)
    delivered = [hub.dispatch(event_id, {'type': 'seat_changed', 'version': 1, 'changes': []})]
    for _ in range(3):
        await asyncio.sleep(0)  # The pump takes frame 1 and stalls inside send_text.
    delivered += [hub.dispatch(event_id, {'type': 'seat_changed', 'version': version, 'changes': []}) for version in range(2, 7)]
    assert delivered == [1, 1, 1, 1, 0, 0], 'one frame in flight plus three queued, then overflow'
    await asyncio.wait_for(pump, timeout=2)
    assert socket.closed_with == CLOSE_RESYNC and hub.connection_count() == 0
    assert socket.sent[0]['version'] == 1
    await hub.shutdown()
