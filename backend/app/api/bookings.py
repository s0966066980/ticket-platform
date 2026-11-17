from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from app.auth import Principal, require_buyer
from app.booking import reserve_order
from app.database import get_session

router = APIRouter(prefix='/events', tags=['booking'])


class Selection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    seat_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=4)
    zone_id: UUID | None = None
    quantity: int | None = Field(default=None, ge=1, le=4, strict=True)

    @model_validator(mode='after')
    def one_selection_mode(self):
        if self.seat_ids is not None:
            if self.zone_id is not None or self.quantity is not None or len(set(self.seat_ids)) != len(self.seat_ids):
                raise ValueError('Provide distinct seat IDs or a zone with quantity')
        elif self.zone_id is None or self.quantity is None:
            raise ValueError('Provide distinct seat IDs or a zone with quantity')
        return self


@router.post('/{event_id}/bookings', status_code=201)
def reserve(
    event_id: UUID, payload: Selection, request: Request,
    principal: Annotated[Principal, Depends(require_buyer)],
    session: Annotated[Session, Depends(get_session)],
    key: Annotated[str, Header(alias='Idempotency-Key', min_length=1, max_length=200)],
):
    result = reserve_order(session, principal, event_id, payload.model_dump(exclude_none=True), key, request.state.request_id)
    return JSONResponse(result.body, status_code=result.status, headers=result.headers)
