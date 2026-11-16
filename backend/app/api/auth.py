from typing import Annotated

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from app.auth import Principal, current_principal, login_user, logout_user, normalize_email, register_user, user_json
from app.database import get_session

router = APIRouter(prefix='/auth', tags=['auth'])


class Credentials(BaseModel):
    model_config = ConfigDict(extra='forbid')
    email: str = Field(max_length=320)
    password: str = Field(min_length=8, max_length=128)

    @field_validator('email')
    @classmethod
    def valid_email(cls, value: str) -> str:
        return normalize_email(value)


@router.post('/register', status_code=201)
def register(payload: Credentials, session: Annotated[Session, Depends(get_session)]):
    return user_json(register_user(session, payload.email, payload.password))


@router.post('/login')
def login(payload: Credentials, session: Annotated[Session, Depends(get_session)]):
    token, user = login_user(session, payload.email, payload.password)
    return {'token': token, 'user': user_json(user)}


@router.get('/me')
def me(principal: Annotated[Principal, Depends(current_principal)]):
    return user_json(principal)


@router.post('/logout', status_code=204)
def logout(principal: Annotated[Principal, Depends(current_principal)], session: Annotated[Session, Depends(get_session)]):
    logout_user(session, principal)
    return Response(status_code=204)
