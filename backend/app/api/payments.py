from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.auth import Principal, require_buyer
from app.database import get_session
from app.payments import initiate_payment, receive_callback

router = APIRouter(tags=['payments'])
SessionDep = Annotated[Session, Depends(get_session)]


class PaymentRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    outcome: Literal['success', 'failure']
    delay_seconds: float = Field(default=0, ge=0)


@router.post('/orders/{order_id}/payments', status_code=202)
def initiate(
    order_id: UUID, payload: PaymentRequest, request: Request,
    principal: Annotated[Principal, Depends(require_buyer)], session: SessionDep,
    key: Annotated[str, Header(alias='Idempotency-Key', min_length=1, max_length=200)],
):
    result = initiate_payment(session, principal, order_id, payload.outcome, payload.delay_seconds, key, request.state.request_id)
    return JSONResponse(result.body, status_code=result.status, headers=result.headers)


@router.post('/payments/callback')
async def callback(
    request: Request, session: SessionDep,
    timestamp: Annotated[str | None, Header(alias='X-Gateway-Timestamp')] = None,
    provided: Annotated[str | None, Header(alias='X-Gateway-Signature')] = None,
):
    # Service signature over the raw body is the only accepted credential here;
    # a user bearer token is never consulted. Verification and settlement run
    # off the event loop because they hold database locks.
    raw = await request.body()
    return await run_in_threadpool(receive_callback, session, request.url.path, raw, timestamp, provided)
