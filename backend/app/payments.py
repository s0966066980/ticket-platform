"""Durable mock payment protocol, settlement and retryable compensation."""
from datetime import timedelta
from hashlib import sha256
import hmac
import json
import logging
from typing import Literal
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.auth import Principal
from app.booking import emit_seat_changes, get_order, load_seat_changes, lock_order_seats, lock_quota
from app.config import get_settings
from app.database import db_now, new_session
from app.errors import AppError
from app.events import lock_event_stream
from app.idempotency import OperationResult, claim_key, complete_key, lock_claim, prepare_claim, reject_key, release_key, request_hash
from app.models import Order, OrderItem, OutboxEvent, Payment, PaymentCallback, Seat, ServiceLog

LOG = logging.getLogger(__name__)


def canonical_bytes(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def signature(secret: str, timestamp: str, method: str, path: str, body: bytes) -> str:
    signed = timestamp.encode() + b'\n' + method.upper().encode() + b'\n' + path.encode() + b'\n' + body
    return hmac.new(secret.encode(), signed, sha256).hexdigest()


def signed_headers(session: Session, method: str, path: str, body: bytes, request_id: str) -> dict:
    timestamp = str(int(db_now(session).timestamp()))
    return {'Content-Type': 'application/json', 'X-Gateway-Timestamp': timestamp, 'X-Gateway-Signature': signature(get_settings().gateway_secret, timestamp, method, path, body), 'X-Request-ID': request_id}


def verify_signature(session: Session, method: str, path: str, body: bytes, timestamp: str | None, provided: str | None) -> None:
    if not timestamp or not provided or len(timestamp) > 20 or len(provided) != 64:
        raise AppError(401, 'INVALID_GATEWAY_SIGNATURE', 'A valid gateway signature is required')
    try:
        sent_at = int(timestamp)
    except ValueError as exc:
        raise AppError(401, 'INVALID_GATEWAY_SIGNATURE', 'A valid gateway signature is required') from exc
    if abs(db_now(session).timestamp() - sent_at) > 300:
        raise AppError(401, 'EXPIRED_GATEWAY_SIGNATURE', 'Gateway delivery signature has expired')
    expected = signature(get_settings().gateway_secret, timestamp, method, path, body)
    if not hmac.compare_digest(expected, provided):
        raise AppError(401, 'INVALID_GATEWAY_SIGNATURE', 'A valid gateway signature is required')


class CallbackPayload(BaseModel):
    model_config = ConfigDict(extra='forbid')
    provider_event_id: str = Field(min_length=1, max_length=200)
    payment_id: UUID
    order_id: UUID
    provider_charge_id: str = Field(min_length=1, max_length=200)
    amount_twd: int = Field(ge=0, strict=True)
    currency: Literal['TWD']
    status: Literal['SUCCEEDED', 'FAILED']
    occurred_at: AwareDatetime
    request_id: str = Field(min_length=1, max_length=128)


def audit(session: Session, request_id: str, service: str, code: str, context: dict, *, level='INFO', now=None):
    session.add(ServiceLog(request_id=request_id, service=service, level=level, code=code, context=context, observed_at=now or db_now(session)))


def _context(session: Session, payment_id: UUID):
    metadata = session.execute(select(Order.user_id, Order.event_id, Order.id).join(Payment, Payment.order_id == Order.id).where(Payment.id == payment_id)).one_or_none()
    if metadata is None:
        raise AppError(404, 'PAYMENT_NOT_FOUND', 'Payment not found')
    lock_quota(session, metadata.user_id, metadata.event_id)
    order = session.scalar(select(Order).where(Order.id == metadata.id).with_for_update().execution_options(populate_existing=True))
    payment = session.scalar(select(Payment).where(Payment.id == payment_id).with_for_update().execution_options(populate_existing=True))
    return order, payment


def initiate_payment(session: Session, principal: Principal, order_id: UUID, outcome: str, delay_seconds: float, key: str, request_id: str) -> OperationResult:
    if principal.role not in {'ADMIN', 'USER'}:
        raise AppError(403, 'FORBIDDEN', 'This role cannot initiate payments')
    prepare_claim(session)
    payload = {'outcome': outcome, 'delay_seconds': float(delay_seconds)}
    claim = claim_key(principal.id, f'payment:{order_id}', key, payload)
    if isinstance(claim, OperationResult):
        return claim
    try:
        record = lock_claim(session, claim)
        existing = get_order(session, principal, order_id)
        lock_quota(session, existing.user_id, existing.event_id)
        order = session.scalar(select(Order).where(Order.id == order_id).with_for_update().execution_options(populate_existing=True))
        now = db_now(session)
        if order.status != 'PENDING_PAYMENT' or order.expires_at <= now:
            raise AppError(409, 'ORDER_NOT_PAYABLE', 'The order is no longer payable')
        if session.scalar(select(Payment.id).where(Payment.order_id == order.id, Payment.status == 'PENDING').limit(1)) is not None:
            raise AppError(409, 'PAYMENT_IN_PROGRESS', 'A payment is already pending; wait for its result')
        payment = Payment(order_id=order.id, status='PENDING', amount_twd=order.total_twd, currency=order.currency, request_id=request_id, next_retry_at=now + timedelta(seconds=5))
        session.add(payment)
        session.flush()
        command = {'payment_id': str(payment.id), 'order_id': str(order.id), 'amount_twd': order.total_twd, 'currency': order.currency, 'outcome': outcome, 'delay_seconds': float(delay_seconds), 'request_id': request_id}
        session.add(OutboxEvent(kind='payment_requested', aggregate_id=payment.id, payload=command, request_id=request_id))
        result = OperationResult(202, {'id': str(payment.id), 'status': payment.status})
        complete_key(session, record, result, order_id=order.id, payment_id=payment.id)
        audit(session, request_id, 'backend', 'PAYMENT_REQUESTED', {'payment_id': str(payment.id), 'order_id': str(order.id)}, now=now)
        session.commit()
        return result
    except AppError as exc:
        session.rollback()
        if exc.code == 'IDEMPOTENCY_IN_PROGRESS':
            raise
        return reject_key(claim, exc, request_id)
    except Exception:
        session.rollback()
        release_key(claim)
        raise


def _queue_reversal(session: Session, payment: Payment, now, reason: str) -> None:
    if payment.status in {'REVERSAL_PENDING', 'REVERSED'}:
        return
    payment.status = 'REVERSAL_PENDING'
    payment.failure_code = reason
    payment.reversal_key = f'reverse:{payment.provider_charge_id}'
    payment.next_retry_at = now
    session.add(OutboxEvent(kind='payment_reversal_requested', aggregate_id=payment.id, payload={'payment_id': str(payment.id), 'provider_charge_id': payment.provider_charge_id, 'idempotency_key': payment.reversal_key}, request_id=payment.request_id))


def process_callback(session: Session, payload: CallbackPayload | dict) -> dict:
    """Called only after signed HTTP verification or authenticated provider query."""
    event = payload if isinstance(payload, CallbackPayload) else CallbackPayload.model_validate(payload)
    normalized = event.model_dump(mode='json')
    digest = request_hash(normalized)
    order, payment = _context(session, event.payment_id)
    existing = session.scalar(select(PaymentCallback).where(PaymentCallback.provider_event_id == event.provider_event_id))
    if existing is not None:
        if existing.payload_hash != digest:
            raise AppError(409, 'CALLBACK_EVENT_CONFLICT', 'This provider event ID has different content')
        result = {'status': 'processed', 'result': existing.result_code, 'payment_id': str(payment.id)}
        session.commit()
        return result
    if event.order_id != order.id or event.amount_twd != payment.amount_twd or event.amount_twd != order.total_twd or event.currency != payment.currency:
        raise AppError(409, 'PAYMENT_MISMATCH', 'Callback does not match the order and amount')
    if payment.provider_charge_id is not None and payment.provider_charge_id != event.provider_charge_id:
        raise AppError(409, 'PROVIDER_CHARGE_CONFLICT', 'The payment is bound to another provider charge')
    payment.provider_charge_id = event.provider_charge_id
    now = db_now(session)
    if event.status == 'FAILED':
        if payment.status == 'PENDING':
            payment.status = 'FAILED'
            payment.failure_code = 'MOCK_FAILURE'
            payment.next_retry_at = None
            result_code = 'PAYMENT_FAILED'
        else:
            result_code = 'FAILURE_ALREADY_TERMINAL'
    else:
        seats = lock_order_seats(session, order)
        stream = lock_event_stream(session, order.event_id)
        now = db_now(session)  # ALL required locks, including stream, have been obtained.
        if payment.status == 'REVERSED':
            result_code = 'ALREADY_REVERSED'
        elif order.status == 'PAID' and order.accepted_payment_id == payment.id:
            result_code = 'ALREADY_PAID'
        elif payment.status == 'REVERSAL_PENDING':
            result_code = 'REVERSAL_PENDING'
        elif order.status == 'PENDING_PAYMENT' and order.expires_at > now and len(seats) == order.ticket_count and all(seat.status == 'HELD' and seat.current_order_id == order.id and seat.held_until > now for seat in seats):
            identifiers = [seat.id for seat in seats]
            changed = list(session.scalars(update(Seat).where(Seat.id.in_(identifiers), Seat.current_order_id == order.id, Seat.status == 'HELD', Seat.held_until > now).values(status='SOLD', held_by=None, held_until=None, version=Seat.version + 1).returning(Seat.id).execution_options(synchronize_session=False)))
            if len(changed) != order.ticket_count:
                raise AppError(409, 'RESERVATION_STATE_CONFLICT', 'The reserved seats could not all be settled')
            session.execute(update(OrderItem).where(OrderItem.order_id == order.id).values(sold_at=now))
            payment.status = 'SUCCEEDED'
            payment.succeeded_at = now
            payment.next_retry_at = None
            order.status = 'PAID'
            order.accepted_payment_id = payment.id
            order.paid_at = now
            emit_seat_changes(session, order.event_id, stream, load_seat_changes(session, changed), event.request_id)
            result_code = 'PAID'
        else:
            payment.succeeded_at = payment.succeeded_at or now
            reason = 'EXTRA_PAYMENT' if order.status == 'PAID' else 'LATE_PAYMENT'
            if order.status == 'PENDING_PAYMENT':
                if order.expires_at <= now:
                    order.status = 'EXPIRED'
                    order.expired_at = now
                else:
                    order.status = 'CANCELED'
                    order.canceled_at = now
                    reason = 'RESERVATION_STATE_CONFLICT'
                identifiers = [seat.id for seat in seats]
                released = list(session.scalars(update(Seat).where(Seat.id.in_(identifiers), Seat.current_order_id == order.id, Seat.status == 'HELD').values(status='AVAILABLE', current_order_id=None, held_by=None, held_until=None, version=Seat.version + 1).returning(Seat.id).execution_options(synchronize_session=False)))
                emit_seat_changes(session, order.event_id, stream, load_seat_changes(session, released), event.request_id)
            _queue_reversal(session, payment, now, reason)
            result_code = reason + '_REVERSAL_PENDING'
    session.add(PaymentCallback(provider_event_id=event.provider_event_id, payment_id=payment.id, payload_hash=digest, payload=normalized, processed_at=now, result_code=result_code, request_id=event.request_id))
    audit(session, event.request_id, 'backend', 'PAYMENT_CALLBACK', {'payment_id': str(payment.id), 'result': result_code}, now=now)
    session.commit()
    return {'status': 'processed', 'result': result_code, 'payment_id': str(payment.id)}


def receive_callback(session: Session, path: str, raw: bytes, timestamp: str | None, provided: str | None) -> dict:
    verify_signature(session, 'POST', path, raw, timestamp, provided)
    try:
        payload = CallbackPayload.model_validate_json(raw)
    except ValidationError as exc:
        raise AppError(422, 'INVALID_CALLBACK', 'The callback payload is invalid') from exc
    return process_callback(session, payload)


def _gateway_request(client: httpx.Client, method: str, path: str, payload: dict | None, request_id: str):
    body = canonical_bytes(payload) if payload is not None else b''
    with new_session() as session:
        headers = signed_headers(session, method, path, body, request_id)
    response = client.request(method, get_settings().gateway_url.rstrip('/') + path, content=body, headers=headers)
    response.raise_for_status()
    return response.json()


def _claim_jobs(limit: int):
    with new_session() as session:
        now = db_now(session)
        jobs = list(session.scalars(select(OutboxEvent).where(OutboxEvent.kind.in_(['payment_requested', 'payment_reversal_requested']), OutboxEvent.published_at.is_(None), OutboxEvent.available_at <= now, (OutboxEvent.lease_until.is_(None) | (OutboxEvent.lease_until <= now))).order_by(OutboxEvent.available_at, OutboxEvent.id).limit(limit).with_for_update(skip_locked=True)))
        result = []
        for job in jobs:
            job.generation += 1
            job.lease_until = now + timedelta(seconds=30)
            result.append((job.id, job.generation, job.kind, dict(job.payload), job.request_id))
        session.commit()
        return result


def _finish_job(identifier, generation, kind, payload, response, request_id):
    payment_id = UUID(payload['payment_id'])
    with new_session() as session:
        order, payment = _context(session, payment_id)
        job = session.scalar(select(OutboxEvent).where(OutboxEvent.id == identifier).with_for_update().execution_options(populate_existing=True))
        if job is None or job.generation != generation or job.published_at is not None:
            return False
        now = db_now(session)
        if kind == 'payment_requested':
            if payment.provider_charge_id is not None and payment.provider_charge_id != response['id']:
                raise ValueError('Provider changed an idempotent charge identity')
            payment.provider_charge_id = response['id']
            if payment.status == 'PENDING':
                payment.next_retry_at = now + timedelta(seconds=5)
        else:
            if response.get('status') not in {'REVERSED', 'NO_CHARGE'}:
                raise ValueError('Provider did not confirm reversal')
            payment.status = 'REVERSED'
            payment.reversed_at = now
            payment.reversal_attempts += 1
            payment.next_retry_at = None
            payment.last_error = None
        job.published_at = now
        job.lease_until = None
        job.last_error = None
        audit(session, request_id, 'worker', 'PAYMENT_JOB_COMPLETED', {'payment_id': str(payment.id), 'kind': kind}, now=now)
        session.commit()
        return True


def _fail_job(identifier, generation, payload, request_id, error):
    with new_session() as session:
        order, payment = _context(session, UUID(payload['payment_id']))
        job = session.scalar(select(OutboxEvent).where(OutboxEvent.id == identifier).with_for_update().execution_options(populate_existing=True))
        if job is None or job.generation != generation or job.published_at is not None:
            return
        now = db_now(session)
        job.attempts += 1
        job.lease_until = None
        job.available_at = now + timedelta(seconds=min(2 ** min(job.attempts, 6), 60))
        job.last_error = type(error).__name__
        if job.kind == 'payment_reversal_requested':
            payment.reversal_attempts += 1
            payment.last_error = job.last_error
            payment.next_retry_at = job.available_at
        audit(session, request_id, 'worker', 'PAYMENT_JOB_RETRY', {'payment_id': str(payment.id), 'kind': job.kind, 'attempt': job.attempts}, level='WARNING', now=now)
        session.commit()


def process_payment_jobs(limit: int = 100, client: httpx.Client | None = None) -> int:
    """Run durable charge/reversal outbox work; network calls hold no DB locks."""
    if client is None:
        with httpx.Client(timeout=10) as owned_client:
            return process_payment_jobs(limit, owned_client)
    completed = 0
    for identifier, generation, kind, payload, request_id in _claim_jobs(limit):
        try:
            if kind == 'payment_requested':
                response = _gateway_request(client, 'POST', '/charges', payload, request_id)
            else:
                path = '/charges/' + quote(payload['provider_charge_id'], safe='') + '/reversal'
                response = _gateway_request(client, 'POST', path, {'idempotency_key': payload['idempotency_key']}, request_id)
            completed += int(_finish_job(identifier, generation, kind, payload, response, request_id))
        except Exception as exc:
            LOG.warning('Payment job retry: %s', type(exc).__name__)
            _fail_job(identifier, generation, payload, request_id, exc)
    return completed


def reconcile_payments(limit: int = 100, client: httpx.Client | None = None) -> int:
    """Recover provider success when its callback was lost; HTTP query is signed."""
    if client is None:
        with httpx.Client(timeout=10) as owned_client:
            return reconcile_payments(limit, owned_client)
    with new_session() as session:
        ids = list(session.scalars(select(Payment.id).where(Payment.status == 'PENDING', Payment.next_retry_at <= db_now(session)).order_by(Payment.next_retry_at, Payment.id).limit(limit)))
    reconciled = 0
    for payment_id in ids:
        with new_session() as session:
            payment = session.get(Payment, payment_id)
            request_id = payment.request_id
        try:
            result = _gateway_request(client, 'GET', '/charges/by-key/' + str(payment_id), None, request_id)
            if result.get('callback'):
                with new_session() as session:
                    process_callback(session, result['callback'])
                reconciled += 1
        except (httpx.HTTPError, AppError, ValueError) as exc:
            LOG.warning('Payment reconciliation retry: %s', type(exc).__name__)
        finally:
            with new_session() as session:
                order, payment = _context(session, payment_id)
                if payment.status == 'PENDING':
                    payment.next_retry_at = db_now(session) + timedelta(seconds=5)
                    session.commit()
    return reconciled
