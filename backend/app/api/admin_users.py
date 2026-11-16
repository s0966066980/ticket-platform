from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import Principal, require_admin, update_user, user_json
from app.database import get_session
from app.models import User

router = APIRouter(prefix='/admin/users', tags=['admin'])


class UserPatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    role: Literal['ADMIN', 'USER', 'VIEWER'] | None = None
    is_active: bool | None = None

    @model_validator(mode='after')
    def require_change(self):
        if not self.model_fields_set or any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError('Provide role or is_active with a non-null value')
        return self


@router.get('')
def users(
    principal: Annotated[Principal, Depends(require_admin)],
    session: Annotated[Session, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: UUID | None = None,
):
    statement = select(User).order_by(User.id).limit(limit + 1)
    if cursor is not None:
        statement = statement.where(User.id > cursor)
    records = list(session.scalars(statement))
    page = records[:limit]
    return {'items': [user_json(user) for user in page], 'next_cursor': str(page[-1].id) if len(records) > limit else None}


@router.patch('/{user_id}')
def edit_user(
    user_id: UUID,
    payload: UserPatch,
    principal: Annotated[Principal, Depends(require_admin)],
    session: Annotated[Session, Depends(get_session)],
):
    return user_json(update_user(session, user_id, role=payload.role, is_active=payload.is_active))
