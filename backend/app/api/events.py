from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from app.auth import Principal, current_principal, require_creator
from app.database import get_session
from app.errors import AppError
from app import events

router = APIRouter(prefix='/events', tags=['events'])
PrincipalDep = Annotated[Principal, Depends(current_principal)]
CreatorDep = Annotated[Principal, Depends(require_creator)]
SessionDep = Annotated[Session, Depends(get_session)]


class ZoneInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    code: str = Field(min_length=1, max_length=50)
    name: str = Field(min_length=1, max_length=100)
    price_twd: int = Field(ge=0, le=2_147_483_647, strict=True)

    @field_validator('code', 'name')
    @classmethod
    def clean_text(cls, value):
        value = value.strip()
        if not value:
            raise ValueError('Value cannot be blank')
        return value


class EventCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default='', max_length=10000)
    category: str = Field(default='', max_length=100)
    venue: str = Field(default='', max_length=200)
    sales_start: AwareDatetime
    sales_end: AwareDatetime
    performance_start: AwareDatetime | None = None
    performance_end: AwareDatetime | None = None
    zones: list[ZoneInput] = Field(min_length=1, max_length=1000)

    @field_validator('title')
    @classmethod
    def nonempty_title(cls, value):
        value = value.strip()
        if not value:
            raise ValueError('Title cannot be blank')
        return value

    @model_validator(mode='after')
    def valid_event(self):
        if self.sales_end <= self.sales_start:
            raise ValueError('Sales end must be after sales start')
        start, end = self.performance_start, self.performance_end
        if (start is None) != (end is None) or (start is not None and end <= start):
            raise ValueError('Provide both performance times with end after start')
        if len({zone.code for zone in self.zones}) != len(self.zones):
            raise ValueError('Zone codes must be unique within the activity')
        return self


class EventPatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10000)
    category: str | None = Field(default=None, max_length=100)
    venue: str | None = Field(default=None, max_length=200)
    sales_start: AwareDatetime | None = None
    sales_end: AwareDatetime | None = None
    performance_start: AwareDatetime | None = None
    performance_end: AwareDatetime | None = None
    zones: list[ZoneInput] | None = Field(default=None, min_length=1, max_length=1000)

    @field_validator('title')
    @classmethod
    def nonempty_title(cls, value):
        if value is not None:
            value = value.strip()
            if not value:
                raise ValueError('Title cannot be blank')
        return value

    @model_validator(mode='after')
    def meaningful_patch(self):
        if not self.model_fields_set:
            raise ValueError('Provide at least one field to update')
        for field in self.model_fields_set - {'performance_start', 'performance_end'}:
            if getattr(self, field) is None:
                raise ValueError(f'{field} cannot be null')
        if self.zones is not None and len({zone.code for zone in self.zones}) != len(self.zones):
            raise ValueError('Zone codes must be unique within the activity')
        return self


def expected_version(value: Annotated[str | None, Header(alias='If-Match')] = None) -> int:
    if value is None:
        raise AppError(428, 'PRECONDITION_REQUIRED', 'Provide the current activity version in If-Match')
    version = value[1:-1] if len(value) >= 2 and value.startswith('"') and value.endswith('"') else value
    if not version.isascii() or not version.isdigit() or len(version) > 10:
        raise AppError(400, 'INVALID_VERSION', 'If-Match must contain a nonnegative activity version')
    return int(version)


@router.get('')
def catalog(
    principal: PrincipalDep, session: SessionDep,
    category: str | None = None, venue: str | None = None,
    status: Literal['DRAFT', 'PUBLISHED'] | None = None,
    sales_state: Literal['UPCOMING', 'OPEN', 'ENDED'] | None = None,
    sort: Literal['created_at', '-created_at', 'sales_start', '-sales_start', 'title', '-title'] = '-created_at',
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    snapshot_id: UUID | None = None, cursor: str | None = None,
):
    return events.list_events(session, principal, category=category, venue=venue, status=status, sales_state=sales_state, sort=sort, limit=limit, snapshot_id=snapshot_id, cursor=cursor)


@router.post('', status_code=201)
def create(payload: EventCreate, principal: CreatorDep, session: SessionDep):
    return events.create_event(session, principal, payload.model_dump())


@router.get('/{event_id}')
def detail(event_id: UUID, principal: PrincipalDep, session: SessionDep):
    return events.event_json(session, events.get_event(session, event_id, principal))


@router.patch('/{event_id}')
def edit(event_id: UUID, payload: EventPatch, principal: CreatorDep, session: SessionDep, expected: Annotated[int, Depends(expected_version)]):
    return events.update_event(session, principal, event_id, expected, payload.model_dump(exclude_unset=True))


@router.post('/{event_id}/publish')
def publish(event_id: UUID, principal: CreatorDep, session: SessionDep, expected: Annotated[int, Depends(expected_version)]):
    return events.publish_event(session, principal, event_id, expected)


@router.delete('/{event_id}', status_code=204)
def delete(event_id: UUID, principal: CreatorDep, session: SessionDep):
    events.delete_event(session, principal, event_id)
    return Response(status_code=204)


@router.get('/{event_id}/seats')
def seats(event_id: UUID, principal: PrincipalDep, zone_id: UUID | None = None):
    return events.seat_snapshot(event_id, principal, zone_id)


@router.get('/{event_id}/version')
def version(event_id: UUID, principal: PrincipalDep, session: SessionDep):
    return events.event_version(session, principal, event_id)
