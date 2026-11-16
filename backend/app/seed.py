"""Idempotently create the three configured development/test accounts."""
from uuid import uuid4

from argon2 import PasswordHasher
from sqlalchemy.dialects.postgresql import insert

from app.config import get_settings
from app.database import new_session
from app.models import User


def seed_accounts() -> None:
    settings = get_settings()
    accounts = [
        (settings.seed_admin_email.strip().lower(), settings.seed_admin_password, 'ADMIN'),
        (settings.seed_user_email.strip().lower(), settings.seed_user_password, 'USER'),
        (settings.seed_viewer_email.strip().lower(), settings.seed_viewer_password, 'VIEWER'),
    ]
    if len({email for email, _, _ in accounts}) != 3 or any(not email or not password for email, password, _ in accounts):
        raise ValueError('Seed accounts require three distinct emails and nonempty passwords')
    hasher = PasswordHasher()
    with new_session() as session, session.begin():
        for email, password, role in accounts:
            session.execute(insert(User).values(
                id=uuid4(), email=email, password_hash=hasher.hash(password),
                role=role, is_active=True, auth_version=0,
            ).on_conflict_do_nothing())


if __name__ == '__main__':
    seed_accounts()
    print('Configured seed accounts are present.')
