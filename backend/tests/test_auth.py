import pytest

pytestmark = pytest.mark.integration


def register(client, email="buyer@example.test", password="Buyer-test-123!", **extra):
    return client.post("/api/v1/auth/register", json={"email": email, "password": password, **extra})


def login(client, email="buyer@example.test", password="Buyer-test-123!"):
    return client.post("/api/v1/auth/login", json={"email": email, "password": password})


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_registered_user_can_login_and_its_token_stops_working_after_logout(client):
    registered = register(client)
    assert registered.status_code == 201
    assert registered.json()["role"] == "USER"
    logged_in = login(client)
    assert logged_in.status_code == 200
    token = logged_in.json()["token"]
    assert client.get("/api/v1/auth/me", headers=bearer(token)).json()["email"] == "buyer@example.test"
    assert client.post("/api/v1/auth/logout", headers=bearer(token)).status_code == 204
    assert client.get("/api/v1/auth/me", headers=bearer(token)).status_code == 401


def seed_test_accounts():
    import os
    from pathlib import Path
    import subprocess
    import sys
    completed = subprocess.run([sys.executable, '-m', 'app.seed'], cwd=Path(__file__).parents[1], env=os.environ.copy(), capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr


def test_registration_rejects_role_injection_and_duplicate_normalized_email(client):
    assert register(client, role='ADMIN').status_code == 422
    created = register(client, email=' Buyer@Example.test ')
    assert created.status_code == 201
    assert created.json()['email'] == 'buyer@example.test'
    assert 'password_hash' not in created.json()
    assert register(client).status_code == 409


@pytest.mark.parametrize('email,password,expected', [
    ('admin@example.test', 'Admin-test-123!', 200),
    ('user@example.test', 'User-test-123!', 403),
    ('viewer@example.test', 'Viewer-test-123!', 403),
])
def test_only_admin_can_list_users(client, email, password, expected):
    seed_test_accounts()
    token = login(client, email, password).json()['token']
    response = client.get('/api/v1/admin/users', headers=bearer(token))
    assert response.status_code == expected
    if expected == 200:
        assert {user['role'] for user in response.json()['items']} == {'ADMIN', 'USER', 'VIEWER'}
        assert all('password_hash' not in user for user in response.json()['items'])


@pytest.mark.parametrize('patch', [{'role': 'VIEWER'}, {'is_active': False}])
def test_role_change_or_disable_revokes_all_existing_tokens_immediately(client, patch):
    seed_test_accounts()
    admin = login(client, 'admin@example.test', 'Admin-test-123!').json()['token']
    first = login(client, 'user@example.test', 'User-test-123!').json()
    second = login(client, 'user@example.test', 'User-test-123!').json()['token']
    changed = client.patch('/api/v1/admin/users/' + first['user']['id'], headers=bearer(admin), json=patch)
    assert changed.status_code == 200
    for token in [first['token'], second]:
        assert client.get('/api/v1/auth/me', headers=bearer(token)).status_code == 401
    relogin = login(client, 'user@example.test', 'User-test-123!')
    if patch.get('is_active') is False:
        assert relogin.status_code == 401
    else:
        assert relogin.status_code == 200
        assert relogin.json()['user']['role'] == 'VIEWER'


def test_missing_and_invalid_credentials_are_rejected(client):
    assert client.get('/api/v1/auth/me').status_code == 401
    assert client.get('/api/v1/auth/me', headers=bearer('not-a-token')).status_code == 401
    assert login(client, 'unknown@example.test', 'Wrong-test-123!').status_code == 401


def test_expired_session_fails_without_waiting_for_real_time(client, db_session):
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import update
    from app.models import Session as LoginSession
    register(client)
    token = login(client).json()['token']
    db_session.execute(update(LoginSession).values(expires_at=datetime.now(timezone.utc) - timedelta(days=1)))
    db_session.commit()
    assert client.get('/api/v1/auth/me', headers=bearer(token)).status_code == 401
