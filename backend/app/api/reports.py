from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from starlette.responses import FileResponse
from starlette.types import Receive, Scope, Send

from app.auth import Principal, current_principal
from app.database import get_session
from app.reports import build_report

router = APIRouter(prefix='/events', tags=['reports'])


class TemporaryReportResponse(FileResponse):
    """Remove the export on completion, send failure, or client cancellation."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            Path(self.path).unlink(missing_ok=True)


@router.get('/{event_id}/report', response_class=FileResponse)
def event_report(
    event_id: UUID,
    principal: Annotated[Principal, Depends(current_principal)],
    session: Annotated[Session, Depends(get_session)],
):
    # Authorization's read-only autobegin no longer needs a pooled connection.
    # Release it before acquiring the dedicated export snapshot, avoiding every
    # simultaneous download retaining one connection while waiting for another.
    session.rollback()
    path = build_report(session, event_id, principal)
    return TemporaryReportResponse(
        path,
        filename=f'sales-{event_id}.xlsx',
        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'},
    )
