"""WebSocket seat feed route. Token is sent in the first frame, never in the URL."""
from importlib import util
from uuid import UUID

from fastapi import APIRouter, WebSocket

from app.feed import serve_subscriber

ws_router = APIRouter(tags=['feed'])


def _connection_delta():
    # Optional observability gauge; the feed must work without the telemetry module.
    if util.find_spec('app.observability') is None:
        return None
    from app.observability import connection_delta
    return connection_delta


@ws_router.websocket('/ws/events/{event_id}')
async def seat_feed(websocket: WebSocket, event_id: UUID):
    await serve_subscriber(websocket, event_id, _connection_delta())
