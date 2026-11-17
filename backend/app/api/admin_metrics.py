"""Admin observability endpoints: per-second metrics, alert history, request logs."""
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from app.auth import Principal, require_admin
from app.database import get_session
from app.errors import AppError
from app.models import AccessLog, Alert, ServiceLog
from app.observability import _TRACE_ID, alert_json, window_metrics
from app.pagination import encode_cursor, parse_cursor

router = APIRouter(prefix='/admin', tags=['admin'])


@router.get('/metrics')
def metrics(
    principal: Annotated[Principal, Depends(require_admin)],
    session: Annotated[Session, Depends(get_session)],
):
    return window_metrics(session)


@router.get('/alerts')
def alerts(
    principal: Annotated[Principal, Depends(require_admin)],
    session: Annotated[Session, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
):
    """Fired and resolved alerts, newest first; pending (not yet sustained) conditions are internal."""
    statement = select(Alert).where(Alert.status.in_(['FIRING', 'RESOLVED'])).order_by(Alert.triggered_at.desc(), Alert.id.desc()).limit(limit + 1)
    position = parse_cursor(cursor)
    if position is not None:
        statement = statement.where(tuple_(Alert.triggered_at, Alert.id) < position)
    records = list(session.scalars(statement))
    page = records[:limit]
    next_cursor = encode_cursor(page[-1].triggered_at, page[-1].id) if len(records) > limit else None
    return {'items': [alert_json(alert) for alert in page], 'next_cursor': next_cursor}


def _access_entry(row: AccessLog) -> dict:
    if row.error_kind:
        level = 'ERROR'
    elif row.business_outcome:
        level = 'WARNING'
    else:
        level = 'INFO'
    return {
        'service': 'api', 'level': level, 'code': row.error_kind or row.business_outcome or f'HTTP_{row.status_code}',
        'observed_at': row.observed_at.isoformat(), 'event_id': str(row.event_id), 'request_id': row.request_id,
        'context': {
            'method': row.method, 'route_template': row.route_template, 'status_code': row.status_code,
            'duration_ms': round(row.duration_us / 1000, 3), 'instance_id': row.instance_id, 'clock_error_us': row.clock_error_us,
        },
    }


def _service_entry(row: ServiceLog) -> dict:
    return {
        'service': row.service, 'level': row.level, 'code': row.code, 'observed_at': row.observed_at.isoformat(),
        'event_id': str(row.event_id), 'request_id': row.request_id,
        'context': {**row.context, 'span_id': row.span_id, 'parent_span_id': row.parent_span_id},
    }


@router.get('/logs/{request_id}')
def request_logs(
    request_id: str,
    principal: Annotated[Principal, Depends(require_admin)],
    session: Annotated[Session, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    cursor: str | None = None,
):
    """Access and service log rows of one request_id across services, oldest first."""
    if not _TRACE_ID.fullmatch(request_id):
        raise AppError(422, 'VALIDATION_ERROR', 'Invalid request_id')
    position = parse_cursor(cursor)
    access = select(AccessLog).where(AccessLog.request_id == request_id).order_by(AccessLog.observed_at, AccessLog.event_id).limit(limit + 1)
    service = select(ServiceLog).where(ServiceLog.request_id == request_id).order_by(ServiceLog.observed_at, ServiceLog.event_id).limit(limit + 1)
    if position is not None:
        access = access.where(tuple_(AccessLog.observed_at, AccessLog.event_id) > position)
        service = service.where(tuple_(ServiceLog.observed_at, ServiceLog.event_id) > position)
    rows = list(session.scalars(access)) + list(session.scalars(service))
    rows.sort(key=lambda row: (row.observed_at, row.event_id))
    page = rows[:limit]
    next_cursor = encode_cursor(page[-1].observed_at, page[-1].event_id) if len(rows) > limit else None
    return {'items': [_access_entry(row) if isinstance(row, AccessLog) else _service_entry(row) for row in page], 'next_cursor': next_cursor}
