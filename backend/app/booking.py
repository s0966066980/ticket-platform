"""Atomic reservation and order-finalization services for API and workers."""
from datetime import timedelta
from uuid import UUID

import json

from sqlalchemy import and_, func, or_, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import Principal, ensure_owner
from app.config import get_settings
from app.database import db_now, new_session
from app.errors import AppError
from app.events import get_event
from app.idempotency import Claim, OperationResult, claim_key, complete_key, lock_claim, prepare_claim, reject_key, release_key
from app.pagination import encode_cursor, parse_cursor
from app.models import Order, OrderItem, OutboxEvent, Payment, Seat, SeatEvent, UserEventQuota, Zone


def lock_quota(session: Session, user_id: UUID, event_id: UUID) -> UserEventQuota:
    session.execute(insert(UserEventQuota).values(user_id=user_id, event_id=event_id, max_tickets=4).on_conflict_do_nothing(index_elements=['user_id', 'event_id']))
    return session.scalar(select(UserEventQuota).where(UserEventQuota.user_id == user_id, UserEventQuota.event_id == event_id).with_for_update().execution_options(populate_existing=True))


def lock_order_seats(session: Session, order: Order) -> list[Seat]:
    identifiers = select(OrderItem.seat_id).where(OrderItem.order_id == order.id)
    return list(session.scalars(select(Seat).where(Seat.id.in_(identifiers)).order_by(Seat.id).with_for_update().execution_options(populate_existing=True)))


def load_seat_changes(session: Session, seat_ids: list[UUID]) -> list[dict]:
    """Fresh Core projection: safe even if old ORM Seat objects are cached."""
    if not seat_ids:
        return []
    rows = session.execute(select(Seat.id, Seat.zone_id, Seat.row_label, Seat.seat_number, Seat.status, Seat.held_until, Seat.version).where(Seat.id.in_(seat_ids)).order_by(Seat.id))
    return [{
        'id': str(row.id), 'zone_id': str(row.zone_id), 'row_label': row.row_label,
        'seat_number': row.seat_number, 'status': row.status,
        'held_until': row.held_until.isoformat() if row.held_until else None,
        'version': row.version,
    } for row in rows]


def emit_seat_changes(session: Session, event_id: UUID, changes: list[dict], request_id: str, *, require_on_sale: bool = False) -> dict | None:
    """Allocate the next activity version and record the change in the shortest possible serialized section.

    Every buyer of one activity serializes here, so the section is three round
    trips: (1) an atomic UPDATE of ``event_streams`` takes the row lock and hands
    back the version, (2) one statement re-reads the activity under that lock
    (a concurrent soft delete or unpublish inserts nothing and we roll back) and
    writes ``seat_events`` plus the Outbox row, (3) the caller's COMMIT. All
    other work, including the HTTP response body, must be flushed before.
    """
    if not changes:
        return None
    session.flush()  # Nothing of ours may be sent while the version lock is held.
    version = session.scalar(text('UPDATE event_streams SET current_version = current_version + 1, updated_at = clock_timestamp() WHERE event_id = :event_id RETURNING current_version'), {'event_id': event_id})
    if version is None:
        raise AppError(500, 'EVENT_STREAM_MISSING', 'Activity state is incomplete')
    payload = {'type': 'seat_changed', 'event_id': str(event_id), 'version': version, 'changes': changes}
    guard = " AND e.status = 'PUBLISHED' AND e.sales_start <= clock_timestamp() AND clock_timestamp() < e.sales_end" if require_on_sale else ''
    inserted = session.scalar(text(f"""
        WITH ok AS (SELECT 1 FROM events e WHERE e.id = :event_id AND e.deleted_at IS NULL{guard}),
             audit AS (INSERT INTO seat_events (event_id, version, changes, occurred_at, request_id)
                       SELECT :event_id, :version, CAST(:payload AS jsonb), clock_timestamp(), :request_id FROM ok RETURNING version),
             outbox AS (INSERT INTO outbox_events (id, kind, aggregate_id, aggregate_version, payload, request_id, available_at, attempts, generation, created_at)
                        SELECT gen_random_uuid(), 'seat_changed', :event_id, :version, CAST(:payload AS jsonb), :request_id, clock_timestamp(), 0, 0, clock_timestamp() FROM ok RETURNING id)
        SELECT count(*) FROM audit"""), {'event_id': event_id, 'version': version, 'payload': json.dumps(payload, separators=(',', ':')), 'request_id': request_id})
    if not inserted:
        # Failure path only: tell a concurrent soft delete (404) apart from an
        # activity that merely stopped selling (409).
        if session.scalar(text('SELECT 1 FROM events WHERE id = :event_id AND deleted_at IS NULL'), {'event_id': event_id}) is None:
            raise AppError(404, 'NOT_FOUND', 'Activity not found')
        raise AppError(409, 'NOT_ON_SALE', 'This activity is not currently accepting new orders')
    return payload


def order_json(session: Session, order: Order, *, now=None) -> dict:
    now = now or db_now(session)
    status = 'EXPIRED' if order.status == 'PENDING_PAYMENT' and order.expires_at <= now else order.status
    items = list(session.scalars(select(OrderItem).where(OrderItem.order_id == order.id).order_by(OrderItem.id)))
    payments = list(session.scalars(select(Payment).where(Payment.order_id == order.id).order_by(Payment.created_at, Payment.id)))
    return {
        'id': str(order.id), 'event_id': str(order.event_id), 'user_id': str(order.user_id),
        'status': status, 'ticket_count': order.ticket_count, 'subtotal_twd': order.subtotal_twd,
        'fee_twd': order.fee_twd, 'total_twd': order.total_twd, 'currency': order.currency,
        'expires_at': order.expires_at.isoformat(), 'created_at': order.created_at.isoformat(),
        'paid_at': order.paid_at.isoformat() if order.paid_at else None,
        'items': [{'seat_id': str(item.seat_id), 'zone_code_snapshot': item.zone_code_snapshot, 'row_label_snapshot': item.row_label_snapshot, 'seat_number_snapshot': item.seat_number_snapshot, 'unit_price_twd': item.unit_price_twd} for item in items],
        'payments': [{'id': str(payment.id), 'status': payment.status} for payment in payments],
    }


def get_order(session: Session, principal: Principal, order_id: UUID) -> Order:
    if principal.role not in {'ADMIN', 'USER'}:
        raise AppError(403, 'FORBIDDEN', 'This role cannot access orders')
    order = session.scalar(select(Order).where(Order.id == order_id).execution_options(populate_existing=True))
    if order is None:
        raise AppError(404, 'NOT_FOUND', 'Order not found')
    ensure_owner(principal, order.user_id, hide=True)
    return order


def list_orders(session: Session, principal: Principal, *, limit=20, cursor: str | None = None) -> dict:
    """Newest first. Order ids are random UUIDs, so age comes from created_at with id as tiebreak."""
    statement = select(Order).order_by(Order.created_at.desc(), Order.id.desc()).limit(limit + 1)
    if principal.role != 'ADMIN':
        statement = statement.where(Order.user_id == principal.id)
    position = parse_cursor(cursor)
    if position is not None:
        statement = statement.where(tuple_(Order.created_at, Order.id) < position)
    records = list(session.scalars(statement))
    page = records[:limit]
    now = db_now(session)
    next_cursor = encode_cursor(page[-1].created_at, page[-1].id) if len(records) > limit else None
    return {'items': [order_json(session, record, now=now) for record in page], 'next_cursor': next_cursor}


def _eligible(now):
    return or_(Seat.status == 'AVAILABLE', and_(Seat.status == 'HELD', Seat.held_until <= now))


def _normalize_selection(selection: dict) -> dict:
    named = selection.get('seat_ids')
    zone = selection.get('zone_id')
    quantity = selection.get('quantity')
    if named is not None:
        if zone is not None or quantity is not None or not 1 <= len(named) <= 4 or len(set(named)) != len(named):
            raise AppError(422, 'INVALID_SELECTION', 'Select 1–4 distinct seats or a zone and quantity')
        return {'seat_ids': sorted(str(identifier) for identifier in named)}
    if zone is None or type(quantity) is not int or not 1 <= quantity <= 4:
        raise AppError(422, 'INVALID_SELECTION', 'Select 1–4 distinct seats or a zone and quantity')
    return {'zone_id': str(zone), 'quantity': quantity}


def _reserve(session: Session, principal: Principal, event_id: UUID, selection: dict, claim: Claim, request_id: str) -> OperationResult:
    record = lock_claim(session, claim)
    get_event(session, event_id, principal)  # Existence/visibility before inserting the quota FK.
    quota = lock_quota(session, principal.id, event_id)
    if 'seat_ids' in selection:
        identifiers = [UUID(identifier) for identifier in selection['seat_ids']]
        quantity = len(identifiers)
        seats = list(session.scalars(select(Seat).where(Seat.event_id == event_id, Seat.id.in_(identifiers)).order_by(Seat.id).with_for_update().execution_options(populate_existing=True)))
        if len(seats) != quantity:
            raise AppError(404, 'SEAT_NOT_FOUND', 'One or more seats do not belong to this activity')
    else:
        quantity = selection['quantity']
        zone_id = UUID(selection['zone_id'])
        if session.scalar(select(Zone.id).where(Zone.id == zone_id, Zone.event_id == event_id)) is None:
            raise AppError(404, 'ZONE_NOT_FOUND', 'Zone not found')
        seats = list(session.scalars(select(Seat).where(Seat.event_id == event_id, Seat.zone_id == zone_id, _eligible(func.clock_timestamp())).order_by(Seat.id).limit(quantity).with_for_update(skip_locked=True).execution_options(populate_existing=True)))
        if len(seats) != quantity:
            visible = session.scalar(select(func.count()).select_from(Seat).where(Seat.event_id == event_id, Seat.zone_id == zone_id, _eligible(func.clock_timestamp())))
            if visible >= quantity:
                raise AppError(409, 'SEAT_CONTENTION', 'Seats are temporarily busy; retry', {'retryable': True})
            raise AppError(409, 'INSUFFICIENT_SEATS', 'The zone does not have enough available seats')
    # The quota row lock and the seat row locks are the protection against
    # over-quota and double-selling. Every rejection is decided here, before the
    # per-activity version lock, so competing buyers of one activity only
    # serialize on the short section that assigns a version to a real change.
    now = db_now(session)
    event = get_event(session, event_id, principal)  # Fresh SELECT + populate_existing, no Event lock.
    if event.status != 'PUBLISHED' or not event.sales_start <= now < event.sales_end:
        raise AppError(409, 'NOT_ON_SALE', 'This activity is not currently accepting new orders')
    if any(seat.status != 'AVAILABLE' and not (seat.status == 'HELD' and seat.held_until <= now) for seat in seats):
        raise AppError(409, 'SEAT_UNAVAILABLE', 'One or more selected seats are no longer available')
    used = session.scalar(select(func.coalesce(func.sum(Order.ticket_count), 0)).where(Order.user_id == principal.id, Order.event_id == event_id, or_(Order.status == 'PAID', and_(Order.status == 'PENDING_PAYMENT', Order.expires_at > now))))
    if used + quantity > quota.max_tickets:
        raise AppError(409, 'QUOTA_EXCEEDED', 'At most four active or paid tickets are allowed per account and activity')
    zones = {zone.id: zone for zone in session.scalars(select(Zone).where(Zone.id.in_({seat.zone_id for seat in seats})))}
    subtotal = sum(zones[seat.zone_id].price_twd for seat in seats)
    fee = (subtotal * 5 + 50) // 100
    order = Order(event_id=event_id, user_id=principal.id, status='PENDING_PAYMENT', ticket_count=quantity, subtotal_twd=subtotal, fee_twd=fee, total_twd=subtotal + fee, currency='TWD', expires_at=now + timedelta(seconds=get_settings().hold_seconds))
    session.add(order)
    session.flush()
    identifiers = [seat.id for seat in seats]
    changed = list(session.scalars(update(Seat).where(Seat.id.in_(identifiers), Seat.event_id == event_id, _eligible(now)).values(status='HELD', current_order_id=order.id, held_by=principal.id, held_until=order.expires_at, version=Seat.version + 1).returning(Seat.id).execution_options(synchronize_session=False)))
    if len(changed) != quantity:
        raise AppError(409, 'SEAT_UNAVAILABLE', 'The selected seats could not all be reserved')
    for seat in seats:
        zone = zones[seat.zone_id]
        session.add(OrderItem(order_id=order.id, event_id=event_id, seat_id=seat.id, zone_code_snapshot=zone.code, row_label_snapshot=seat.row_label, seat_number_snapshot=seat.seat_number, unit_price_twd=zone.price_twd))
    session.flush()
    result = OperationResult(201, order_json(session, order, now=now))
    complete_key(session, record, result, order_id=order.id)
    # Last: the per-activity version section re-checks publication, sale window
    # and soft deletion under the lock, so a concurrent delete rolls all of this back.
    emit_seat_changes(session, event_id, load_seat_changes(session, changed), request_id, require_on_sale=True)
    session.commit()
    return result


def reserve_order(session: Session, principal: Principal, event_id: UUID, selection: dict, key: str, request_id: str) -> OperationResult:
    if principal.role not in {'ADMIN', 'USER'}:
        raise AppError(403, 'FORBIDDEN', 'This role cannot reserve seats')
    selection = _normalize_selection(selection)
    prepare_claim(session)
    claim = claim_key(principal.id, f'booking:{event_id}', key, selection)
    if isinstance(claim, OperationResult):
        return claim
    for attempt in range(3):
        try:
            return _reserve(session, principal, event_id, selection, claim, request_id)
        except AppError as exc:
            session.rollback()
            if exc.code == 'IDEMPOTENCY_IN_PROGRESS':
                raise
            return reject_key(claim, exc, request_id)
        except SQLAlchemyError as exc:
            session.rollback()
            if getattr(getattr(exc, 'orig', None), 'sqlstate', None) in {'40P01', '40001'} and attempt < 2:
                continue
            release_key(claim)
            raise AppError(503, 'DATABASE_UNAVAILABLE', 'Reservation could not be completed; retry with the same key') from exc
    raise AssertionError('Unreachable reservation retry state')


def _finalize_pending(session: Session, order_id: UUID, *, principal: Principal | None, expiry_only: bool, request_id: str) -> tuple[dict, bool]:
    metadata = session.execute(select(Order.user_id, Order.event_id).where(Order.id == order_id)).one_or_none()
    if metadata is None:
        raise AppError(404, 'NOT_FOUND', 'Order not found')
    if principal is not None:
        if principal.role not in {'ADMIN', 'USER'}:
            raise AppError(403, 'FORBIDDEN', 'This role cannot cancel orders')
        ensure_owner(principal, metadata.user_id, hide=True)
    lock_quota(session, metadata.user_id, metadata.event_id)
    order = session.scalar(select(Order).where(Order.id == order_id).with_for_update().execution_options(populate_existing=True))
    if order.status != 'PENDING_PAYMENT':
        if order.status == 'PAID' and not expiry_only:
            raise AppError(409, 'PAID_ORDER_CANNOT_CANCEL', 'Paid-order cancellation is not supported')
        result = order_json(session, order)
        session.commit()
        return result, False
    locked_seats = lock_order_seats(session, order)
    now = db_now(session)  # After the quota, order and seat locks.
    expired = order.expires_at <= now
    if expiry_only and not expired:
        result = order_json(session, order, now=now)
        session.commit()
        return result, False
    if expired:
        order.status = 'EXPIRED'
        order.expired_at = now
    else:
        order.status = 'CANCELED'
        order.canceled_at = now
    # A previous order can never release a seat already reclaimed by a newer one.
    changed = list(session.scalars(update(Seat).where(Seat.id.in_([seat.id for seat in locked_seats]), Seat.event_id == order.event_id, Seat.current_order_id == order.id, Seat.status == 'HELD').values(status='AVAILABLE', current_order_id=None, held_by=None, held_until=None, version=Seat.version + 1).returning(Seat.id).execution_options(synchronize_session=False)))
    result = order_json(session, order, now=now)
    emit_seat_changes(session, order.event_id, load_seat_changes(session, changed), request_id)
    session.commit()
    return result, True


def cancel_order(session: Session, principal: Principal, order_id: UUID, request_id: str) -> dict:
    return _finalize_pending(session, order_id, principal=principal, expiry_only=False, request_id=request_id)[0]


def expire_order(session: Session, order_id: UUID, request_id: str = 'expiry-worker') -> bool:
    return _finalize_pending(session, order_id, principal=None, expiry_only=True, request_id=request_id)[1]


def expire_due_orders(limit: int = 100, event_id: UUID | None = None) -> int:
    """Candidate discovery takes no order locks; each transition uses normal order.

    Separate worker sessions can discover the same IDs. The quota/order locks and
    terminal-state checks make duplicate execution harmless.
    """
    with new_session() as session:
        statement = select(Order.id).where(Order.status == 'PENDING_PAYMENT', Order.expires_at <= func.clock_timestamp()).order_by(Order.expires_at, Order.id).limit(limit)
        if event_id is not None:
            statement = statement.where(Order.event_id == event_id)
        identifiers = list(session.scalars(statement))
    processed = 0
    for identifier in identifiers:
        with new_session() as session:
            processed += int(expire_order(session, identifier))
    return processed
