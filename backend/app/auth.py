"""Session authentication and shared authorization boundaries.

Token authorization always reads current database state. The Principal DTO never
contains a token, password hash or mutable ORM identity shared across services.
"""
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
import secrets
from typing import Annotated
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import db_now, get_session
from app.errors import AppError
from app.models import Session as LoginSession, User

PASSWORD_HASHER = PasswordHasher()
_BEARER = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    id: UUID
    email: str
    role: str
    is_active: bool
    auth_version: int
    session_id: UUID

    @property
    def user_id(self) -> UUID:
        return self.id


def user_json(user: User | Principal) -> dict:
    return {"id": str(user.id), "email": user.email, "role": user.role, "is_active": user.is_active}


def normalize_email(email: str) -> str:
    normalized = email.strip().lower()
    if len(normalized) > 320 or normalized.count('@') != 1 or any(char.isspace() for char in normalized):
        raise ValueError('Enter a valid email address')
    local, domain = normalized.split('@')
    if not local or not domain or '.' not in domain or domain.startswith('.') or domain.endswith('.'):
        raise ValueError('Enter a valid email address')
    return normalized


def register_user(session: Session, email: str, password: str) -> User:
    user = User(email=normalize_email(email), password_hash=PASSWORD_HASHER.hash(password), role='USER', is_active=True, auth_version=0)
    session.add(user)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        if session.scalar(select(User.id).where(func.lower(User.email) == user.email)) is not None:
            raise AppError(409, 'EMAIL_EXISTS', 'An account with this email already exists') from exc
        raise
    return user


def login_user(session: Session, email: str, password: str) -> tuple[str, User]:
    # Serialize login with role/active updates so a new session cannot capture a
    # stale auth_version and silently become valid after concurrent changes.
    user = session.scalar(select(User).where(func.lower(User.email) == normalize_email(email)).with_for_update().execution_options(populate_existing=True))
    valid = False
    if user is not None:
        try:
            valid = PASSWORD_HASHER.verify(user.password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            valid = False
    if user is None or not valid or not user.is_active:
        session.rollback()
        raise AppError(401, 'INVALID_CREDENTIALS', 'Email or password is invalid')
    token = secrets.token_urlsafe(32)
    now = db_now(session)
    session.add(LoginSession(
        user_id=user.id, token_hash=sha256(token.encode()).hexdigest(),
        auth_version=user.auth_version,
        expires_at=now + timedelta(seconds=get_settings().session_ttl_seconds),
    ))
    session.commit()
    return token, user


def authenticate_token(session: Session, token: str) -> Principal:
    if not token or len(token) > 512:
        raise AppError(401, 'UNAUTHENTICATED', 'A valid login is required')
    token_hash = sha256(token.encode()).hexdigest()
    row = session.execute(select(User, LoginSession.id).join(LoginSession, LoginSession.user_id == User.id).where(
        LoginSession.token_hash == token_hash,
        LoginSession.revoked_at.is_(None),
        LoginSession.expires_at > func.clock_timestamp(),
        LoginSession.auth_version == User.auth_version,
        User.is_active.is_(True),
    ).execution_options(populate_existing=True)).first()
    if row is None:
        raise AppError(401, 'UNAUTHENTICATED', 'A valid login is required')
    user, session_id = row
    return Principal(id=user.id, email=user.email, role=user.role, is_active=user.is_active, auth_version=user.auth_version, session_id=session_id)


def current_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_BEARER)],
    session: Annotated[Session, Depends(get_session)],
) -> Principal:
    if credentials is None or credentials.scheme.lower() != 'bearer':
        raise AppError(401, 'UNAUTHENTICATED', 'A valid login is required')
    principal = authenticate_token(session, credentials.credentials)
    # Authentication performed only reads and Principal copies immutable values.
    # Release this connection before catalog/seat snapshots or durable request
    # claims open independent transactions, including when the pool is saturated.
    session.rollback()
    return principal


# Stable alias used by feature modules that name their dependency by user role.
get_current_user = current_principal


def require_roles(*roles: str):
    def dependency(principal: Annotated[Principal, Depends(current_principal)]) -> Principal:
        if principal.role not in roles:
            raise AppError(403, 'FORBIDDEN', 'This role cannot perform this operation')
        return principal
    return dependency


require_admin = require_roles('ADMIN')
require_creator = require_roles('ADMIN', 'USER')
require_buyer = require_creator


def ensure_owner(principal: Principal, owner_id: UUID, *, hide: bool = False) -> None:
    if principal.role != 'ADMIN' and principal.id != owner_id:
        if hide:
            raise AppError(404, 'NOT_FOUND', 'Resource not found')
        raise AppError(403, 'FORBIDDEN', 'Only the owner or Admin can perform this operation')


def logout_user(session: Session, principal: Principal) -> None:
    record = session.get(LoginSession, principal.session_id)
    if record is not None:
        record.revoked_at = db_now(session)
        session.commit()


def update_user(session: Session, user_id: UUID, *, role: str | None = None, is_active: bool | None = None) -> User:
    user = session.scalar(select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True))
    if user is None:
        raise AppError(404, 'NOT_FOUND', 'User not found')
    changed = False
    if role is not None and role != user.role:
        user.role = role
        changed = True
    if is_active is not None and is_active != user.is_active:
        user.is_active = is_active
        changed = True
    if changed:
        user.auth_version += 1
    session.commit()
    return user
