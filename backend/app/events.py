"""Activity lifecycle, fixed catalog pagination and coherent seat projections."""
import base64
from datetime import timedelta
import json
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.auth import Principal, ensure_owner
from app.config import get_settings
from app.database import db_now, get_engine
from app.errors import AppError
from app.models import Event, EventStream, ImportJob, ImportRow, ListSnapshot, ListSnapshotItem, Order, Seat, Zone

FROZEN_FIELDS = {'zones', 'sales_start', 'sales_end'}


def ensure_can_manage(principal: Principal, owner_id: UUID) -> None:
    if principal.role not in {'ADMIN', 'USER'}:
        raise AppError(403, 'FORBIDDEN', 'This role cannot manage activities')
    ensure_owner(principal, owner_id)


def get_event(session: Session, event_id: UUID, principal: Principal, *, manage: bool = False, lock: bool = False) -> Event:
    # Always SELECT + populate_existing: callers may already have a stale ORM
    # Event from before waiting for the stream lock. READ COMMITTED is required
    # for a fresh post-lock check; do not replace this with Session.get().
    statement = select(Event).where(Event.id == event_id, Event.deleted_at.is_(None)).execution_options(populate_existing=True)
    if lock:
        # key_share=True without read=True is FOR NO KEY UPDATE on PostgreSQL.
        statement = statement.with_for_update(key_share=True)
    event = session.scalar(statement)
    if event is None:
        raise AppError(404, 'NOT_FOUND', 'Activity not found')
    if manage:
        ensure_can_manage(principal, event.owner_id)
    elif event.status != 'PUBLISHED' and principal.role != 'ADMIN' and principal.id != event.owner_id:
        raise AppError(404, 'NOT_FOUND', 'Activity not found')
    elif event.status != 'PUBLISHED' and principal.role == 'VIEWER':
        raise AppError(404, 'NOT_FOUND', 'Activity not found')
    return event


def lock_event_stream(session: Session, event_id: UUID) -> EventStream:
    stream = session.scalar(select(EventStream).where(EventStream.event_id == event_id).with_for_update().execution_options(populate_existing=True))
    if stream is None:
        raise AppError(500, 'EVENT_STREAM_MISSING', 'Activity state is incomplete')
    return stream


def event_json(session: Session, event: Event, zones=None) -> dict:
    if zones is None:
        zones = list(session.scalars(select(Zone).where(Zone.event_id == event.id).order_by(Zone.code, Zone.id)))
    return {
        'id': str(event.id), 'owner_id': str(event.owner_id), 'title': event.title,
        'description': event.description, 'category': event.category, 'venue': event.venue,
        'status': event.status, 'version': event.version,
        'sales_start': event.sales_start.isoformat(), 'sales_end': event.sales_end.isoformat(),
        'performance_start': event.performance_start.isoformat() if event.performance_start else None,
        'performance_end': event.performance_end.isoformat() if event.performance_end else None,
        'created_at': event.created_at.isoformat(),
        'zones': [{'id': str(zone.id), 'code': zone.code, 'name': zone.name, 'price_twd': zone.price_twd} for zone in zones],
    }


def create_event(session: Session, principal: Principal, payload: dict) -> dict:
    if principal.role not in {'ADMIN', 'USER'}:
        raise AppError(403, 'FORBIDDEN', 'This role cannot create activities')
    values = dict(payload)
    zones = values.pop('zones')
    event = Event(owner_id=principal.id, **values)
    session.add(event)
    session.flush()
    session.add(EventStream(event_id=event.id))
    for zone in zones:
        session.add(Zone(event_id=event.id, **zone))
    session.flush()
    result = event_json(session, event)
    session.commit()
    return result


def require_version(event: Event, expected: int) -> None:
    if event.version != expected:
        raise AppError(412, 'VERSION_CONFLICT', 'Activity changed; reload before editing', {'current_version': event.version})


def _validate_window(values: dict) -> None:
    if values['sales_end'] <= values['sales_start']:
        raise AppError(422, 'INVALID_SALES_WINDOW', 'Sales end must be after sales start')
    start, end = values.get('performance_start'), values.get('performance_end')
    if (start is None) != (end is None) or (start is not None and end <= start):
        raise AppError(422, 'INVALID_PERFORMANCE_WINDOW', 'Provide both performance times, with end after start')


def _replace_zones(session: Session, event: Event, zones: list[dict]) -> None:
    active_job = session.scalar(select(ImportJob.id).where(ImportJob.event_id == event.id, ImportJob.status.in_(['QUEUED', 'RUNNING'])).limit(1))
    if active_job is not None:
        raise AppError(409, 'IMPORT_IN_PROGRESS', 'Wait for the import before changing zones')
    existing = {zone.code: zone for zone in session.scalars(select(Zone).where(Zone.event_id == event.id))}
    incoming = {zone['code']: zone for zone in zones}
    for code, zone in existing.items():
        if code in incoming:
            zone.name = incoming[code]['name']
            zone.price_twd = incoming[code]['price_twd']
        else:
            used = session.scalar(select(Seat.id).where(Seat.zone_id == zone.id).limit(1))
            staged = session.scalar(select(ImportRow.job_id).where(ImportRow.resolved_zone_id == zone.id).limit(1))
            if used is not None or staged is not None:
                raise AppError(409, 'ZONE_IN_USE', 'A zone referenced by imported seats cannot be removed')
            session.delete(zone)
    for code, values in incoming.items():
        if code not in existing:
            session.add(Zone(event_id=event.id, **values))


def update_event(session: Session, principal: Principal, event_id: UUID, expected: int, payload: dict) -> dict:
    event = get_event(session, event_id, principal, manage=True, lock=True)
    require_version(event, expected)
    if event.status == 'PUBLISHED' and FROZEN_FIELDS.intersection(payload):
        raise AppError(409, 'PUBLISHED_CONFIGURATION_FROZEN', 'Published seat configuration, pricing and sales times are fixed')
    window = {key: payload.get(key, getattr(event, key)) for key in ('sales_start', 'sales_end', 'performance_start', 'performance_end')}
    _validate_window(window)
    if 'zones' in payload:
        _replace_zones(session, event, payload['zones'])
    for key, value in payload.items():
        if key != 'zones':
            setattr(event, key, value)
    event.version += 1
    session.flush()
    result = event_json(session, event)
    session.commit()
    return result


def publish_event(session: Session, principal: Principal, event_id: UUID, expected: int) -> dict:
    event = get_event(session, event_id, principal, manage=True, lock=True)
    require_version(event, expected)
    if event.status == 'PUBLISHED':
        return event_json(session, event)
    active_job = session.scalar(select(ImportJob.id).where(ImportJob.event_id == event_id, ImportJob.status.in_(['QUEUED', 'RUNNING'])).limit(1))
    if active_job is not None:
        raise AppError(409, 'IMPORT_IN_PROGRESS', 'Wait for all accepted imports to finish')
    if session.scalar(select(Seat.id).where(Seat.event_id == event_id).limit(1)) is None:
        raise AppError(409, 'NO_SEATS', 'Import at least one valid seat before publishing')
    event.status = 'PUBLISHED'
    event.version += 1
    session.flush()
    result = event_json(session, event)
    session.commit()
    return result


def delete_event(session: Session, principal: Principal, event_id: UUID) -> None:
    event = get_event(session, event_id, principal, manage=True, lock=True)
    lock_event_stream(session, event_id)
    # Booking takes this same stream lock, then rechecks deleted_at before insert.
    if session.scalar(select(Order.id).where(Order.event_id == event_id).limit(1)) is not None:
        raise AppError(409, 'ACTIVITY_HAS_ORDERS', 'Activities with order history cannot be deleted')
    if session.scalar(select(ImportJob.id).where(ImportJob.event_id == event_id, ImportJob.status.in_(['QUEUED', 'RUNNING'])).limit(1)) is not None:
        raise AppError(409, 'IMPORT_IN_PROGRESS', 'Wait for the import before deleting')
    event.deleted_at = db_now(session)
    event.version += 1
    session.commit()


def _catalog_statement(principal: Principal, filters: dict, sort: str, now):
    statement = select(Event).where(Event.deleted_at.is_(None))
    if principal.role == 'VIEWER':
        statement = statement.where(Event.status == 'PUBLISHED')
    elif principal.role != 'ADMIN':
        statement = statement.where(or_(Event.status == 'PUBLISHED', Event.owner_id == principal.id))
    if filters['category']:
        statement = statement.where(Event.category == filters['category'])
    if filters['venue']:
        # Autoescape prevents user %/_ from becoming pattern operators.
        statement = statement.where(Event.venue.icontains(filters['venue'], autoescape=True))
    if filters['status']:
        statement = statement.where(Event.status == filters['status'])
    sale_state = filters['sales_state']
    if sale_state == 'UPCOMING':
        statement = statement.where(Event.sales_start > now)
    elif sale_state == 'OPEN':
        statement = statement.where(Event.status == 'PUBLISHED', Event.sales_start <= now, Event.sales_end > now)
    elif sale_state == 'ENDED':
        statement = statement.where(Event.sales_end <= now)
    column = {'created_at': Event.created_at, 'sales_start': Event.sales_start, 'title': Event.title}[sort.lstrip('-')]
    return statement.order_by(column.desc() if sort.startswith('-') else column.asc(), Event.id.asc())


def _cursor(snapshot_id: UUID, position: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({'snapshot': str(snapshot_id), 'position': position}, separators=(',', ':')).encode()).decode().rstrip('=')


def _parse_cursor(cursor: str) -> tuple[UUID, int]:
    try:
        if len(cursor) > 512:
            raise ValueError
        value = json.loads(base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4)))
        if not isinstance(value, dict) or not isinstance(value.get('snapshot'), str):
            raise ValueError
        identifier = UUID(value['snapshot'])
        position = value['position']
        if type(position) is not int or position < 0:
            raise ValueError
        return identifier, position
    except (ValueError, KeyError, TypeError, UnicodeError) as exc:
        raise AppError(422, 'INVALID_CURSOR', 'Pagination cursor is invalid') from exc


def _make_snapshot(principal: Principal, filters: dict, sort: str, page_size: int) -> UUID:
    # A separate transaction is needed: authentication has already issued a read
    # on the request session, too late to change that transaction's isolation.
    engine = get_engine().execution_options(isolation_level='REPEATABLE READ')
    with Session(engine, expire_on_commit=False) as session, session.begin():
        now = db_now(session)
        snapshot = ListSnapshot(session_id=principal.session_id, user_id=principal.id, auth_version=principal.auth_version, filter_spec=filters, sort_spec={'sort': sort}, page_size=page_size, total_count=0, expires_at=now + timedelta(seconds=get_settings().snapshot_ttl_seconds))
        session.add(snapshot)
        session.flush()
        stream = session.scalars(_catalog_statement(principal, filters, sort, now).execution_options(yield_per=500))
        position = 0
        for partition in stream.partitions(500):
            ids = [event.id for event in partition]
            grouped = {identifier: [] for identifier in ids}
            for zone in session.scalars(select(Zone).where(Zone.event_id.in_(ids)).order_by(Zone.code, Zone.id)):
                grouped[zone.event_id].append(zone)
            session.execute(ListSnapshotItem.__table__.insert(), [
                {'snapshot_id': snapshot.id, 'position': position + index, 'event_id': event.id, 'projection': event_json(session, event, grouped[event.id])}
                for index, event in enumerate(partition)
            ])
            position += len(partition)
        snapshot.total_count = position
        identifier = snapshot.id
    return identifier


def list_events(session: Session, principal: Principal, *, category=None, venue=None, status=None, sales_state=None, sort='-created_at', limit=20, snapshot_id=None, cursor=None) -> dict:
    filters = {'category': category or None, 'venue': venue or None, 'status': status or None, 'sales_state': sales_state or None}
    position = 0
    if cursor:
        cursor_snapshot, position = _parse_cursor(cursor)
        if snapshot_id is not None and cursor_snapshot != snapshot_id:
            raise AppError(422, 'CURSOR_SNAPSHOT_MISMATCH', 'Cursor belongs to a different snapshot')
        snapshot_id = cursor_snapshot
    if snapshot_id is None:
        snapshot_id = _make_snapshot(principal, filters, sort, limit)
    snapshot = session.scalar(select(ListSnapshot).where(ListSnapshot.id == snapshot_id).execution_options(populate_existing=True))
    if snapshot is None or snapshot.session_id != principal.session_id or snapshot.user_id != principal.id or snapshot.auth_version != principal.auth_version:
        raise AppError(404, 'NOT_FOUND', 'Catalog snapshot not found')
    if snapshot.expires_at <= db_now(session):
        raise AppError(410, 'SNAPSHOT_EXPIRED', 'Refresh the activity list to start a new snapshot')
    if snapshot.filter_spec != filters or snapshot.sort_spec != {'sort': sort} or snapshot.page_size != limit:
        raise AppError(409, 'SNAPSHOT_QUERY_CHANGED', 'Refresh before changing catalog filters or page size')
    if position > snapshot.total_count:
        raise AppError(422, 'INVALID_CURSOR', 'Pagination cursor is outside this snapshot')
    rows = list(session.scalars(select(ListSnapshotItem).where(ListSnapshotItem.snapshot_id == snapshot.id, ListSnapshotItem.position >= position).order_by(ListSnapshotItem.position).limit(limit)))
    next_position = position + len(rows)
    return {'items': [row.projection for row in rows], 'snapshot_id': str(snapshot.id), 'total_count': snapshot.total_count, 'next_cursor': _cursor(snapshot.id, next_position) if next_position < snapshot.total_count else None}


def seat_snapshot(event_id: UUID, principal: Principal, zone_id: UUID | None = None) -> dict:
    engine = get_engine().execution_options(isolation_level='REPEATABLE READ')
    with Session(engine) as session:
        get_event(session, event_id, principal)
        row = session.execute(select(EventStream.current_version, func.clock_timestamp()).where(EventStream.event_id == event_id)).one_or_none()
        if row is None:
            raise AppError(500, 'EVENT_STREAM_MISSING', 'Activity state is incomplete')
        version, now = row
        if zone_id is not None and session.scalar(select(Zone.id).where(Zone.id == zone_id, Zone.event_id == event_id)) is None:
            raise AppError(404, 'NOT_FOUND', 'Zone not found')
        statement = select(Seat).where(Seat.event_id == event_id)
        if zone_id is not None:
            statement = statement.where(Seat.zone_id == zone_id)
        items = []
        for seat in session.scalars(statement.order_by(Seat.zone_id, Seat.row_label, Seat.seat_number, Seat.id).execution_options(yield_per=1000)):
            expired = seat.status == 'HELD' and seat.held_until <= now
            items.append({'id': str(seat.id), 'zone_id': str(seat.zone_id), 'row_label': seat.row_label, 'seat_number': seat.seat_number, 'status': 'AVAILABLE' if expired else seat.status, 'held_until': seat.held_until.isoformat() if seat.held_until and not expired else None})
        return {'version': version, 'server_time': now.isoformat(), 'items': items}


def event_version(session: Session, principal: Principal, event_id: UUID) -> dict:
    get_event(session, event_id, principal)
    version = session.scalar(select(EventStream.current_version).where(EventStream.event_id == event_id))
    if version is None:
        raise AppError(500, 'EVENT_STREAM_MISSING', 'Activity state is incomplete')
    return {'version': version}
