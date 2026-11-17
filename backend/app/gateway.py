"""Independent mock payment gateway, run as ``uvicorn app.gateway:app`` on port 8001.

It shares the PostgreSQL server with the backend but reads and writes only
``gateway.charges`` (model GatewayCharge); it never touches orders or seats.
Every request from the backend worker is signed with the shared GATEWAY_SECRET
(see app.payments.signature): ``POST /charges`` (idempotent on payment_key),
``GET /charges/by-key/{payment_key}`` (includes the ``callback`` payload once the
charge has settled so reconciliation can replay it) and
``POST /charges/{charge_id}/reversal`` (idempotent on idempotency_key). Only
``GET /health`` is unauthenticated.

A charge settles once ``delay_seconds`` have elapsed. Its callback is persisted
in the same row (next_callback_at, callback_attempts, lease_generation,
lease_until, callback_delivered_at) and delivered to CALLBACK_URL by the
in-process loop ``deliver_due_callbacks`` every GATEWAY_POLL_SECONDS with
exponential backoff; only a 2xx answer marks it delivered.
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import logging
import re
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.database import db_now, new_session
from app.errors import AppError, register_error_handlers
from app.models import GatewayCharge
from app.payments import canonical_bytes, signed_headers, verify_signature

LOG = logging.getLogger(__name__)
LEASE_SECONDS = 30
FINAL_OUTCOME = {'success': 'SUCCESS', 'failure': 'FAILURE'}
CALLBACK_STATUS = {'SUCCESS': 'SUCCEEDED', 'FAILURE': 'FAILED'}
_REQUEST_ID_PATTERN = re.compile(r'^[A-Za-z0-9_.:-]{1,128}$')


class ChargeRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    payment_id: UUID
    order_id: UUID
    amount_twd: int = Field(ge=0, strict=True)
    currency: Literal['TWD']
    outcome: Literal['success', 'failure']
    delay_seconds: float = Field(default=0, ge=0)
    request_id: str = Field(min_length=1, max_length=128)


class ReversalRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    idempotency_key: str = Field(min_length=1, max_length=200)


def _settle(charge: GatewayCharge, now) -> None:
    if charge.outcome == 'PENDING' and charge.next_callback_at <= now:
        charge.outcome = {'SUCCEEDED': 'SUCCESS', 'FAILED': 'FAILURE'}[charge.callback_payload['status']]


def charge_json(charge: GatewayCharge) -> dict:
    body = {
        'id': str(charge.id), 'payment_key': charge.payment_key, 'order_reference': charge.order_reference,
        'amount_twd': charge.amount_twd, 'currency': charge.currency, 'outcome': charge.outcome,
        'provider_event_id': charge.provider_event_id, 'callback_attempts': charge.callback_attempts,
        'callback_delivered_at': charge.callback_delivered_at.isoformat() if charge.callback_delivered_at else None,
        'reversed_at': charge.reversed_at.isoformat() if charge.reversed_at else None,
    }
    if charge.outcome != 'PENDING':
        body['callback'] = charge.callback_payload
    return body


def _verified(session: Session, request: Request, raw: bytes, model=None):
    verify_signature(session, request.method, request.url.path, raw, request.headers.get('X-Gateway-Timestamp'), request.headers.get('X-Gateway-Signature'))
    if model is None:
        return None
    try:
        return model.model_validate_json(raw)
    except ValidationError as exc:
        raise AppError(422, 'VALIDATION_ERROR', 'Request validation failed', [{'loc': list(item['loc']), 'type': item['type'], 'message': item['msg']} for item in exc.errors()]) from exc


def _create_charge(request: Request, raw: bytes):
    with new_session() as session:
        payload = _verified(session, request, raw, ChargeRequest)
        now = db_now(session)
        charge_id = uuid4()
        settle_at = now + timedelta(seconds=payload.delay_seconds)
        callback = {
            'provider_event_id': 'evt_' + uuid4().hex, 'payment_id': str(payload.payment_id), 'order_id': str(payload.order_id),
            'provider_charge_id': str(charge_id), 'amount_twd': payload.amount_twd, 'currency': payload.currency,
            'status': CALLBACK_STATUS[FINAL_OUTCOME[payload.outcome]], 'occurred_at': settle_at.isoformat(), 'request_id': payload.request_id,
        }
        inserted = session.scalar(insert(GatewayCharge).values(
            id=charge_id, payment_key=str(payload.payment_id), order_reference=str(payload.order_id),
            amount_twd=payload.amount_twd, currency=payload.currency,
            outcome=FINAL_OUTCOME[payload.outcome] if payload.delay_seconds == 0 else 'PENDING',
            provider_event_id=callback['provider_event_id'], callback_payload=callback,
            next_callback_at=settle_at, request_id=payload.request_id,
        ).on_conflict_do_nothing(index_elements=['payment_key']).returning(GatewayCharge.id))
        session.commit()
        charge = session.scalar(select(GatewayCharge).where(GatewayCharge.payment_key == str(payload.payment_id)))
        if inserted is None and (charge.amount_twd != payload.amount_twd or charge.order_reference != str(payload.order_id)):
            raise AppError(409, 'CHARGE_CONFLICT', 'This payment key was already charged with different content')
        return JSONResponse(charge_json(charge), status_code=201 if inserted else 200)


def _get_charge(request: Request, payment_key: str):
    with new_session() as session:
        _verified(session, request, b'')
        charge = session.scalar(select(GatewayCharge).where(GatewayCharge.payment_key == payment_key).with_for_update())
        if charge is None:
            raise AppError(404, 'NOT_FOUND', 'No charge for this payment key')
        _settle(charge, db_now(session))
        body = charge_json(charge)
        session.commit()
        return body


def _reverse_charge(request: Request, charge_id: UUID, raw: bytes):
    with new_session() as session:
        payload = _verified(session, request, raw, ReversalRequest)
        charge = session.scalar(select(GatewayCharge).where(GatewayCharge.id == charge_id).with_for_update())
        if charge is None:
            return {'status': 'NO_CHARGE', 'charge_id': str(charge_id), 'reversed_at': None}
        if charge.reversal_key is not None and charge.reversal_key != payload.idempotency_key:
            raise AppError(409, 'REVERSAL_KEY_CONFLICT', 'This charge was reversed under a different key')
        if charge.reversal_key is None:
            charge.reversal_key = payload.idempotency_key
            charge.reversed_at = db_now(session)
        body = {'status': 'REVERSED', 'charge_id': str(charge.id), 'reversed_at': charge.reversed_at.isoformat()}
        session.commit()
        return body


def deliver_due_callbacks(client: httpx.Client | None = None, limit: int = 100) -> int:
    """One pass of the durable callback loop; returns how many were acknowledged.

    Due rows are leased with a generation so a crashed or duplicate deliverer can
    neither double-mark nor lose a callback. The HTTP call holds no DB lock.
    """
    if client is None:
        with httpx.Client(timeout=10) as owned:
            return deliver_due_callbacks(owned, limit)
    settings = get_settings()
    path = urlsplit(settings.callback_url).path
    with new_session() as session:
        now = db_now(session)
        due = session.scalars(select(GatewayCharge).where(
            GatewayCharge.callback_delivered_at.is_(None), GatewayCharge.next_callback_at <= now,
            or_(GatewayCharge.lease_until.is_(None), GatewayCharge.lease_until <= now),
        ).order_by(GatewayCharge.next_callback_at, GatewayCharge.id).limit(limit).with_for_update(skip_locked=True))
        claimed = []
        for charge in due:
            _settle(charge, now)
            charge.lease_generation += 1
            charge.lease_until = now + timedelta(seconds=LEASE_SECONDS)
            claimed.append((charge.id, charge.lease_generation, dict(charge.callback_payload), charge.request_id))
        session.commit()
    delivered = 0
    for identifier, generation, payload, request_id in claimed:
        body = canonical_bytes(payload)
        with new_session() as session:
            headers = signed_headers(session, 'POST', path, body, request_id)
        try:
            response = client.post(settings.callback_url, content=body, headers=headers)
            error = None if 200 <= response.status_code < 300 else f'HTTP_{response.status_code}'
        except httpx.HTTPError as exc:
            error = type(exc).__name__
        with new_session() as session:
            charge = session.scalar(select(GatewayCharge).where(GatewayCharge.id == identifier).with_for_update().execution_options(populate_existing=True))
            if charge is None or charge.lease_generation != generation or charge.callback_delivered_at is not None:
                continue
            now = db_now(session)
            charge.callback_attempts += 1
            charge.lease_until = None
            if error is None:
                charge.callback_delivered_at = now
                delivered += 1
            else:
                charge.next_callback_at = now + timedelta(seconds=min(2 ** min(charge.callback_attempts, 6), 60))
                LOG.warning('Callback retry for charge %s after %s (attempt %s)', identifier, error, charge.callback_attempts)
            session.commit()
    return delivered


async def _callback_loop(stop: asyncio.Event) -> None:
    poll = max(get_settings().gateway_poll_seconds, 0.1)
    with httpx.Client(timeout=10) as client:
        while not stop.is_set():
            try:
                await asyncio.to_thread(deliver_due_callbacks, client)
            except Exception:  # The loop must survive database outages.
                LOG.exception('Callback delivery pass failed')
            try:
                await asyncio.wait_for(stop.wait(), timeout=poll)
            except TimeoutError:
                continue


@asynccontextmanager
async def lifespan(_: FastAPI):
    stop = asyncio.Event()
    task = asyncio.create_task(_callback_loop(stop))
    try:
        yield
    finally:
        stop.set()
        await asyncio.gather(task, return_exceptions=True)


def create_gateway() -> FastAPI:
    gateway = FastAPI(title='Mock Payment Gateway', version='0.1.0', lifespan=lifespan)
    register_error_handlers(gateway)

    @gateway.middleware('http')
    async def request_context(request, call_next):
        provided = request.headers.get('X-Request-ID', '')
        request.state.request_id = provided if _REQUEST_ID_PATTERN.fullmatch(provided) else str(uuid4())
        response = await call_next(request)
        response.headers['X-Request-ID'] = request.state.request_id
        return response

    @gateway.get('/health', tags=['health'])
    def health():
        return {'status': 'ok'}

    @gateway.post('/charges', status_code=201)
    async def create_charge(request: Request):
        return await run_in_threadpool(_create_charge, request, await request.body())

    @gateway.get('/charges/by-key/{payment_key}')
    async def get_charge(payment_key: str, request: Request):
        return await run_in_threadpool(_get_charge, request, payment_key)

    @gateway.post('/charges/{charge_id}/reversal')
    async def reverse_charge(charge_id: UUID, request: Request):
        return await run_in_threadpool(_reverse_charge, request, charge_id, await request.body())

    return gateway


app = create_gateway()
