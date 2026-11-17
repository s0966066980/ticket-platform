"""Opaque keyset cursors over (timestamp, id) for newest-first collections."""
import base64
from datetime import datetime
from uuid import UUID

from app.errors import AppError


def encode_cursor(moment: datetime, identifier: UUID) -> str:
    """URL-safe cursor pointing just after the row with this (timestamp, id)."""
    return base64.urlsafe_b64encode(f'{moment.isoformat()}|{identifier}'.encode()).decode().rstrip('=')


def parse_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    if cursor is None:
        return None
    try:
        decoded = base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4)).decode()
        moment, identifier = decoded.rsplit('|', 1)
        return datetime.fromisoformat(moment), UUID(identifier)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise AppError(422, 'VALIDATION_ERROR', 'Invalid cursor') from exc
