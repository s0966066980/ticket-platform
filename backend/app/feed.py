"""Seat feed: ordered Outbox publication, Redis Pub/Sub fan-out and bounded WebSocket subscriptions.

Order is defined by the database (``event_streams.current_version``), never by
Redis arrival. Publication is at-least-once: a client applies only
``version == last + 1``, ignores older frames and re-fetches the HTTP snapshot
on any gap, on ``resync_required`` frames and when a heartbeat version differs.

Worker API (synchronous): ``publish_outbox()``.
API process: ``get_hub(app)`` then ``FeedHub.subscribe``/``unsubscribe``.
"""
import asyncio
from dataclasses import dataclass, field
from hashlib import sha256
import json
import logging
from uuid import UUID

from redis import Redis
from redis.asyncio import Redis as AsyncRedis
from redis.exceptions import RedisError
from sqlalchemy import func, select, update
from starlette.websockets import WebSocket, WebSocketState

from app.auth import Principal, authenticate_token
from app.config import get_settings
from app.database import db_now, new_session
from app.errors import AppError
from app.events import get_event
from app.models import EventStream, OutboxEvent, Session as LoginSession, User

LOG = logging.getLogger(__name__)
CHANNEL_PREFIX = 'seat:'
CLOSE_UNAUTHENTICATED = 4001
CLOSE_NOT_FOUND = 4004
CLOSE_PROTOCOL = 4002
CLOSE_RESYNC = 4008  # Queue overflow or feed interruption: the client must resync.
AUTH_TIMEOUT_SECONDS = 5.0


def channel(event_id: UUID | str) -> str:
    return f'{CHANNEL_PREFIX}{event_id}'


def token_digest(token: str) -> str:
    return sha256(token.encode()).hexdigest()


# --------------------------------------------------------------------------- worker side

def _publish_event(client: Redis, event_id: UUID, now_fn) -> int:
    """Publish one activity's unpublished seat events in version order.

    A transaction-scoped advisory lock keeps two workers from interleaving the
    same stream; it never holds the ``event_streams`` row lock during network
    I/O, so booking transactions are not delayed by Redis. Rows are marked
    published only after PUBLISH returned; a crash in between republishes
    (duplicates are harmless because clients ignore versions they have).
    """
    with new_session() as session:
        locked = session.scalar(select(func.pg_try_advisory_xact_lock(func.hashtext('seat-publish:' + str(event_id)))))
        if not locked:
            return 0
        rows = list(session.scalars(select(OutboxEvent).where(
            OutboxEvent.kind == 'seat_changed', OutboxEvent.aggregate_id == event_id, OutboxEvent.published_at.is_(None),
        ).order_by(OutboxEvent.aggregate_version)))
        published = 0
        highest = None
        for row in rows:
            try:
                client.publish(channel(event_id), json.dumps(row.payload, separators=(',', ':')))
            except RedisError as exc:
                row.attempts += 1
                row.last_error = type(exc).__name__
                LOG.warning('Seat event publish failed for %s version %s: %s', event_id, row.aggregate_version, type(exc).__name__)
                break
            row.published_at = now_fn(session)
            row.lease_until = None
            row.last_error = None
            highest = row.aggregate_version
            published += 1
        if highest is not None:
            session.execute(update(EventStream).where(EventStream.event_id == event_id, EventStream.published_version < highest).values(published_version=highest))
        session.commit()
        return published


def publish_outbox(client: Redis | None = None, limit_events: int = 200) -> int:
    """Publish pending ``seat_changed`` Outbox rows for up to ``limit_events`` activities."""
    if client is None:
        client = Redis.from_url(get_settings().redis_url, socket_connect_timeout=2, socket_timeout=2)
        try:
            return publish_outbox(client, limit_events)
        finally:
            client.close()
    with new_session() as session:
        event_ids = list(session.scalars(select(OutboxEvent.aggregate_id).where(
            OutboxEvent.kind == 'seat_changed', OutboxEvent.published_at.is_(None), OutboxEvent.available_at <= func.clock_timestamp(),
        ).distinct().limit(limit_events)))
    return sum(_publish_event(client, event_id, db_now) for event_id in event_ids)


# --------------------------------------------------------------------------- API side

def authenticate_subscriber(token: str, event_id: UUID) -> tuple[Principal, int]:
    """Blocking: validate the session and the activity's visibility, return the current version."""
    with new_session() as session:
        principal = authenticate_token(session, token)
        get_event(session, event_id, principal)
        version = session.scalar(select(EventStream.published_version).where(EventStream.event_id == event_id))
        if version is None:
            raise AppError(500, 'EVENT_STREAM_MISSING', 'Activity state is incomplete')
        return principal, version


def read_version(event_id: UUID) -> int | None:
    """The version a subscriber could have received: what the worker has already handed to Redis.

    ``current_version`` may be ahead by the Outbox poll interval; reporting it in
    heartbeats would make every client resync spuriously while a frame is still
    on its way. A frame a client missed has, by definition, been published, so
    ``published_version`` still exposes real gaps.
    """
    with new_session() as session:
        return session.scalar(select(EventStream.published_version).where(EventStream.event_id == event_id))


def eligible_token_hashes(hashes: set[str]) -> set[str]:
    """One query re-validates every silent subscriber: revoked, expired, deactivated or re-versioned sessions drop out."""
    if not hashes:
        return set()
    with new_session() as session:
        rows = session.scalars(select(LoginSession.token_hash).join(User, User.id == LoginSession.user_id).where(
            LoginSession.token_hash.in_(hashes), LoginSession.revoked_at.is_(None),
            LoginSession.expires_at > func.clock_timestamp(), LoginSession.auth_version == User.auth_version, User.is_active.is_(True),
        ))
        return set(rows)


@dataclass(eq=False)
class Subscription:
    websocket: WebSocket
    event_id: UUID
    token_hash: str
    principal: Principal
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    close_code: int | None = None
    task: asyncio.Task | None = None

    def offer(self, frame: dict) -> bool:
        """Non-blocking enqueue; a full queue marks the slow client for a resync close."""
        if self.close_code is not None:
            return False
        try:
            self.queue.put_nowait(frame)
            return True
        except asyncio.QueueFull:
            self.close_code = CLOSE_RESYNC
            return False

    def wake(self, code: int | None = None) -> None:
        """Record a close code and interrupt the pump, even one stalled inside a send."""
        if code is not None:
            self.close_code = self.close_code or code
        if self.task is not None and not self.task.done():
            self.task.cancel()

    async def pump(self) -> None:
        """Send queued frames in order until closed; exits when the client goes away."""
        try:
            while self.close_code is None:
                frame = await self.queue.get()
                if self.close_code is not None:
                    break
                await self.websocket.send_text(json.dumps(frame, separators=(',', ':')))
        except asyncio.CancelledError:
            pass
        except Exception:
            self.close_code = self.close_code or CLOSE_RESYNC
        finally:
            await self.close()

    async def close(self) -> None:
        if self.websocket.client_state == WebSocketState.CONNECTED and self.websocket.application_state == WebSocketState.CONNECTED:
            try:
                await self.websocket.close(code=self.close_code or 1000)
            except Exception:
                pass


class FeedHub:
    """Per-process fan-out: one Redis pattern subscription, bounded per-connection queues.

    Multiple API replicas each run their own hub; Redis delivers every published
    frame to every replica, and every replica's clients rely on the same
    DB-defined version numbers, so ordering guarantees do not depend on which
    replica a client is attached to.
    """

    def __init__(self, redis_url: str, queue_size: int, verify_interval: float):
        self.redis_url = redis_url
        self.queue_size = max(1, queue_size)
        self.verify_interval = max(0.05, verify_interval)
        self.subscribers: dict[UUID, set[Subscription]] = {}
        # Last version this replica knows per activity: from the handshake, from
        # dispatched frames and from the periodic DB check. Client-requested
        # heartbeats answer from here so 2,000 clients polling every 5 s do not
        # become 400 DB reads per second; the periodic check stays authoritative.
        self.versions: dict[UUID, int] = {}
        self.listener: asyncio.Task | None = None
        self.verifier: asyncio.Task | None = None
        self.listener_connected = False
        self.listener_ready = asyncio.Event()

    async def wait_connected(self, timeout: float = 5.0) -> bool:
        try:
            await asyncio.wait_for(self.listener_ready.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # -- lifecycle
    def _ensure_tasks(self) -> None:
        loop = asyncio.get_running_loop()
        if self.listener is None or self.listener.done():
            self.listener = loop.create_task(self._listen())
        if self.verifier is None or self.verifier.done():
            self.verifier = loop.create_task(self._verify_loop())

    async def shutdown(self) -> None:
        for task in (self.listener, self.verifier):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        for subscriptions in list(self.subscribers.values()):
            for subscription in list(subscriptions):
                subscription.wake(1001)
        self.subscribers.clear()

    # -- subscriptions
    def subscribe(self, websocket: WebSocket, event_id: UUID, token_hash: str, principal: Principal, version: int | None = None) -> Subscription:
        self._ensure_tasks()
        subscription = Subscription(websocket, event_id, token_hash, principal, asyncio.Queue(maxsize=self.queue_size))
        self.subscribers.setdefault(event_id, set()).add(subscription)
        if version is not None:
            self._note_version(event_id, version)
        return subscription

    def _note_version(self, event_id: UUID, version: int) -> None:
        if version > self.versions.get(event_id, -1):
            self.versions[event_id] = version

    def unsubscribe(self, subscription: Subscription) -> None:
        group = self.subscribers.get(subscription.event_id)
        if group is not None:
            group.discard(subscription)
            if not group:
                self.subscribers.pop(subscription.event_id, None)
                self.versions.pop(subscription.event_id, None)

    def connection_count(self) -> int:
        return sum(len(group) for group in self.subscribers.values())

    def dispatch(self, event_id: UUID, frame: dict) -> int:
        if isinstance(frame.get('version'), int) and event_id in self.subscribers:
            self._note_version(event_id, frame['version'])
        delivered = 0
        for subscription in list(self.subscribers.get(event_id, ())):
            if subscription.offer(frame):
                delivered += 1
            else:
                self._drop(subscription)
        return delivered

    def _drop(self, subscription: Subscription) -> None:
        """Wake the pump so it closes the socket with the recorded code."""
        self.unsubscribe(subscription)
        subscription.wake(CLOSE_RESYNC)

    async def heartbeat(self, event_id: UUID, subscription: Subscription | None = None, *, from_db: bool = True) -> None:
        version = await asyncio.to_thread(read_version, event_id) if from_db else self.versions.get(event_id)
        if version is None:
            return
        self._note_version(event_id, version)
        frame = {'type': 'heartbeat', 'version': version}
        targets = [subscription] if subscription is not None else list(self.subscribers.get(event_id, ()))
        for target in targets:
            if not target.offer(frame):
                self._drop(target)

    async def verify_once(self) -> None:
        """Re-check every subscriber's session and push the DB version (at least every verify_interval)."""
        for event_id, group in list(self.subscribers.items()):
            hashes = {subscription.token_hash for subscription in group}
            try:
                eligible = await asyncio.to_thread(eligible_token_hashes, hashes)
            except Exception:
                LOG.exception('Subscriber re-verification failed')
                continue
            for subscription in list(group):
                if subscription.token_hash not in eligible:
                    subscription.close_code = CLOSE_UNAUTHENTICATED
                    self._drop(subscription)
            try:
                await self.heartbeat(event_id)
            except Exception:
                LOG.exception('Heartbeat failed for %s', event_id)

    async def _verify_loop(self) -> None:
        while True:
            await asyncio.sleep(self.verify_interval)
            await self.verify_once()

    # -- Redis
    async def _listen(self) -> None:
        while True:
            client = AsyncRedis.from_url(self.redis_url, socket_connect_timeout=2)
            pubsub = client.pubsub(ignore_subscribe_messages=True)
            try:
                await pubsub.psubscribe(channel('*'))
                self.listener_connected = True
                self.listener_ready.set()
                async for message in pubsub.listen():
                    if message.get('type') != 'pmessage':
                        continue
                    try:
                        name = message['channel']
                        name = name.decode() if isinstance(name, bytes) else name
                        event_id = UUID(name.removeprefix(CHANNEL_PREFIX))
                        data = message['data']
                        frame = json.loads(data.decode() if isinstance(data, bytes) else data)
                    except (ValueError, KeyError, AttributeError):
                        LOG.warning('Ignoring malformed seat feed message')
                        continue
                    self.dispatch(event_id, frame)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning('Seat feed listener disconnected: %s', type(exc).__name__)
            finally:
                self.listener_connected = False
                self.listener_ready.clear()
                try:
                    await pubsub.aclose()
                    await client.aclose()
                except Exception:
                    pass
            # Frames may have been missed while disconnected; the next heartbeat
            # exposes the version difference and clients re-snapshot.
            await asyncio.sleep(1)


def get_hub(app) -> FeedHub:
    hub = getattr(app.state, 'feed_hub', None)
    if hub is None:
        settings = get_settings()
        hub = FeedHub(settings.redis_url, settings.ws_queue_size, settings.ws_version_check_seconds)
        app.state.feed_hub = hub
        handlers = getattr(getattr(app, 'router', None), 'on_shutdown', None)
        if isinstance(handlers, list):  # Starlette runs these at lifespan shutdown.
            handlers.append(hub.shutdown)
    return hub


async def serve_subscriber(websocket: WebSocket, event_id: UUID, connection_delta=None) -> None:
    """Application-level handshake, subscription and client control loop."""
    await websocket.accept()
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=AUTH_TIMEOUT_SECONDS)
        frame = json.loads(raw)
        if not isinstance(frame, dict) or frame.get('type') != 'auth' or not isinstance(frame.get('token'), str):
            raise ValueError('auth frame required')
        token = frame['token']
    except Exception:
        await websocket.close(code=CLOSE_PROTOCOL)
        return
    try:
        principal, version = await asyncio.to_thread(authenticate_subscriber, token, event_id)
    except AppError as exc:
        await websocket.send_text(json.dumps({'type': 'error', 'code': exc.code}))
        await websocket.close(code=CLOSE_UNAUTHENTICATED if exc.status_code == 401 else CLOSE_NOT_FOUND)
        return
    hub = get_hub(websocket.app)
    subscription = hub.subscribe(websocket, event_id, token_digest(token), principal, version)
    if not await hub.wait_connected():
        LOG.warning('Seat feed Redis listener not connected; heartbeats will drive resync')
    if connection_delta is not None:
        connection_delta(+1)
    pump = asyncio.get_running_loop().create_task(subscription.pump())
    subscription.task = pump
    try:
        # Subscribed before auth_ok: the client's snapshot request after this
        # frame cannot race ahead of a frame this connection would miss.
        subscription.offer({'type': 'auth_ok', 'version': version})
        while subscription.close_code is None:
            raw = await websocket.receive_text()
            try:
                request = json.loads(raw)
            except ValueError:
                continue
            if isinstance(request, dict) and request.get('type') == 'heartbeat':
                await hub.heartbeat(event_id, subscription, from_db=False)
    except Exception:
        pass
    finally:
        hub.unsubscribe(subscription)
        if connection_delta is not None:
            connection_delta(-1)
        subscription.wake(1000)
        try:
            await asyncio.wait_for(pump, timeout=1)
        except BaseException:
            pass
        await subscription.close()
