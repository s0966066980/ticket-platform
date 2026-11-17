from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import Principal, current_principal, require_creator
from app.database import get_session
from app import imports
from app.models import ImportError as RowError

router = APIRouter(tags=['imports'])
SessionDep = Annotated[Session, Depends(get_session)]
PrincipalDep = Annotated[Principal, Depends(current_principal)]
CreatorDep = Annotated[Principal, Depends(require_creator)]


@router.post('/events/{event_id}/seats/import', status_code=202)
def upload(
    event_id: UUID, request: Request, principal: CreatorDep, session: SessionDep,
    file: Annotated[UploadFile, File()],
    mode: Annotated[Literal['all_or_nothing', 'skip_invalid'], Form()] = 'all_or_nothing',
):
    try:
        return imports.accept_import(session, principal, event_id, file.file, file.filename or '', mode, request.state.request_id)
    finally:
        file.file.close()


@router.get('/imports/{job_id}')
def status(job_id: UUID, principal: PrincipalDep, session: SessionDep):
    return imports.job_json(imports.get_import(session, principal, job_id))


@router.get('/imports/{job_id}/errors')
def errors(
    job_id: UUID, principal: PrincipalDep, session: SessionDep,
    offset: Annotated[int, Query(ge=0)] = 0,
    cursor: Annotated[int | None, Query(ge=0)] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
):
    imports.get_import(session, principal, job_id)
    if cursor is not None:
        offset = cursor
    rows = session.scalars(select(RowError).where(RowError.job_id == job_id).order_by(
        RowError.row_number.asc().nulls_first(), RowError.id,
    ).offset(offset).limit(limit+1)).all()
    return {'items': [{'row_number': row.row_number, 'source_line': row.source_line,
        'code': row.code, 'reason': row.reason} for row in rows[:limit]],
        'next_cursor': str(offset+limit) if len(rows)>limit else None}


@router.post('/imports/{job_id}/retry', status_code=202)
def retry(job_id: UUID, principal: CreatorDep, session: SessionDep):
    return imports.retry_import(session, principal, job_id)
