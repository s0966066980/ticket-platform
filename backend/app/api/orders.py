from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.auth import Principal, require_buyer
from app.booking import cancel_order, get_order, list_orders, order_json
from app.database import get_session

router = APIRouter(prefix='/orders', tags=['orders'])
PrincipalDep = Annotated[Principal, Depends(require_buyer)]
SessionDep = Annotated[Session, Depends(get_session)]


@router.get('')
def orders(principal: PrincipalDep, session: SessionDep, limit: Annotated[int, Query(ge=1, le=100)] = 20, cursor: Annotated[str | None, Query(max_length=512)] = None):
    """Own orders (all orders for Admin), newest first; next_cursor is opaque."""
    return list_orders(session, principal, limit=limit, cursor=cursor)


@router.get('/{order_id}')
def detail(order_id: UUID, principal: PrincipalDep, session: SessionDep):
    return order_json(session, get_order(session, principal, order_id))


@router.post('/{order_id}/cancel')
def cancel(order_id: UUID, request: Request, principal: PrincipalDep, session: SessionDep):
    return cancel_order(session, principal, order_id, request.state.request_id)
