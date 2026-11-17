"""Durable request claims and transaction-fenced response replay."""
from dataclasses import dataclass, field
from datetime import timedelta
from hashlib import sha256
import json
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import db_now, new_session
from app.errors import AppError
from app.models import IdempotencyKey


@dataclass(frozen=True)
class OperationResult:
    status: int
    body: dict
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Claim:
    id: UUID
    generation: int


def in_progress() -> AppError:
    return AppError(409, 'IDEMPOTENCY_IN_PROGRESS', 'This request is still being processed', headers={'Retry-After': '1'})


def request_hash(payload: dict) -> str:
    return sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()



def prepare_claim(session: Session) -> None:
    """At the HTTP mutation entry, end only its read-only auth transaction.

    Otherwise every concurrent request can retain one pool connection while
    waiting for another for its durable claim. Call before acquiring locks or
    making writes. Principal is an immutable DTO, so it survives rollback.
    """
    if session.new or session.dirty or session.deleted:
        raise RuntimeError('Idempotency claim entry cannot discard pending writes')
    session.rollback()

def claim_key(user_id: UUID, operation: str, key: str, payload: dict) -> Claim | OperationResult:
    if not key or len(key) > 200 or any(ord(char) < 32 for char in key):
        raise AppError(422, 'INVALID_IDEMPOTENCY_KEY', 'Provide an Idempotency-Key of 1 to 200 printable characters')
    digest = request_hash(payload)
    settings = get_settings()
    with new_session() as session:
        now = db_now(session)
        identifier = uuid4()
        created = session.scalar(insert(IdempotencyKey).values(
            id=identifier, user_id=user_id, operation=operation, key=key,
            request_hash=digest, state='IN_PROGRESS', generation=1,
            lease_until=now + timedelta(seconds=settings.idempotency_lease_seconds),
            expires_at=now + timedelta(seconds=settings.idempotency_seconds),
        ).on_conflict_do_nothing(index_elements=['user_id', 'operation', 'key']).returning(IdempotencyKey.id))
        if created is not None:
            session.commit()
            return Claim(created, 1)
        record = session.scalar(select(IdempotencyKey).where(IdempotencyKey.user_id == user_id, IdempotencyKey.operation == operation, IdempotencyKey.key == key))
        if record is None:
            raise in_progress()
        if record.request_hash != digest:
            raise AppError(409, 'IDEMPOTENCY_KEY_REUSED', 'This key was used with different request content')
        if record.state == 'COMPLETED':
            return OperationResult(record.response_status, record.response_body)
        if record.lease_until > db_now(session):
            raise in_progress()
        try:
            record = session.scalar(select(IdempotencyKey).where(IdempotencyKey.id == record.id).with_for_update(nowait=True).execution_options(populate_existing=True))
        except OperationalError as exc:
            if getattr(exc.orig, 'sqlstate', None) == '55P03':
                raise in_progress() from exc
            raise
        if record.state == 'COMPLETED':
            return OperationResult(record.response_status, record.response_body)
        now = db_now(session)
        if record.lease_until > now:
            raise in_progress()
        record.generation += 1
        record.lease_until = now + timedelta(seconds=settings.idempotency_lease_seconds)
        record.expires_at = max(record.expires_at, now + timedelta(seconds=settings.idempotency_seconds))
        claim = Claim(record.id, record.generation)
        session.commit()
        return claim


def lock_claim(session: Session, claim: Claim) -> IdempotencyKey:
    record = session.scalar(select(IdempotencyKey).where(IdempotencyKey.id == claim.id).with_for_update().execution_options(populate_existing=True))
    if record is None or record.generation != claim.generation or record.state != 'IN_PROGRESS' or record.lease_until <= db_now(session):
        raise in_progress()
    return record


def complete_key(session: Session, record: IdempotencyKey, result: OperationResult, *, order_id=None, payment_id=None) -> None:
    """Caller holds the claim lock; response and business changes commit together."""
    record.state = 'COMPLETED'
    record.response_status = result.status
    record.response_body = result.body
    record.order_id = order_id
    record.payment_id = payment_id


def reject_key(claim: Claim, exc: AppError, request_id: str) -> OperationResult:
    body = {'code': exc.code, 'message': exc.message, 'request_id': request_id}
    if exc.details is not None:
        body['details'] = exc.details
    result = OperationResult(exc.status_code, body, exc.headers)
    with new_session() as session:
        record = session.scalar(select(IdempotencyKey).where(IdempotencyKey.id == claim.id).with_for_update().execution_options(populate_existing=True))
        if record is None or record.generation != claim.generation:
            raise in_progress()
        if record.state == 'COMPLETED':
            return OperationResult(record.response_status, record.response_body)
        complete_key(session, record, result)
        session.commit()
    return result


def release_key(claim: Claim) -> None:
    """Best-effort lease release after a rolled-back infrastructure failure.

    Never overwrite COMPLETED if a commit succeeded but its acknowledgement was
    lost. An unresolved server transaction keeps its row lock; NOWAIT leaves it
    alone until normal replay or lease recovery can determine the outcome.
    """
    try:
        with new_session() as session:
            record = session.scalar(select(IdempotencyKey).where(IdempotencyKey.id == claim.id).with_for_update(nowait=True).execution_options(populate_existing=True))
            if record is not None and record.generation == claim.generation and record.state == 'IN_PROGRESS':
                record.lease_until = db_now(session)
                session.commit()
    except SQLAlchemyError:
        return
