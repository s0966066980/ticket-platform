"""Every business route is enumerated from the app itself: a new endpoint that forgets
its authentication dependency, or lets a Viewer mutate, fails here without anyone
having to remember to add a test for it."""
from uuid import uuid4

import pytest
from app.main import create_app

pytestmark = pytest.mark.integration

# Anonymous by design: registration, login, liveness and the signed gateway callback
# (which is covered by signature tests in test_payments.py, not by a user token).
ANONYMOUS = {('POST', '/api/v1/auth/register'), ('POST', '/api/v1/auth/login'), ('POST', '/api/v1/payments/callback')}
# Mutations a Viewer may perform on their own session.
VIEWER_MUTATIONS = {('POST', '/api/v1/auth/logout')}


def business_routes():
    """The published OpenAPI document is the contract, so enumerate from it."""
    routes = []
    for path, operations in create_app().openapi()['paths'].items():
        if path.startswith('/api/v1'):
            for method in sorted(operations):
                routes.append((method.upper(), path))
    assert len(routes) >= 25, 'route enumeration must see the whole API'
    return routes


def concrete(path: str) -> str:
    return path.replace('{event_id}', str(uuid4())).replace('{order_id}', str(uuid4())).replace('{job_id}', str(uuid4())).replace('{user_id}', str(uuid4())).replace('{request_id}', 'probe').replace('{', '').replace('}', '')


def viewer_headers(client, db_session):
    from app.models import User
    credentials = {'email': 'matrix-viewer@example.test', 'password': 'Matrix-test-123!'}
    assert client.post('/api/v1/auth/register', json=credentials).status_code == 201
    db_session.query(User).filter(User.email == credentials['email']).update({'role': 'VIEWER'})
    db_session.commit()
    return {'Authorization': 'Bearer ' + client.post('/api/v1/auth/login', json=credentials).json()['token']}


def test_every_business_route_rejects_anonymous_requests(client):
    offenders = []
    for method, path in business_routes():
        if (method, path) in ANONYMOUS:
            continue
        response = client.request(method, concrete(path))
        if response.status_code != 401:
            offenders.append((method, path, response.status_code))
    assert offenders == [], f'routes reachable without a login: {offenders}'


def test_viewer_cannot_mutate_anything(client, db_session):
    headers = viewer_headers(client, db_session)
    offenders = []
    for method, path in business_routes():
        if method == 'GET' or (method, path) in ANONYMOUS | VIEWER_MUTATIONS:
            continue
        response = client.request(method, concrete(path), headers=headers)
        if response.status_code != 403:
            offenders.append((method, path, response.status_code))
    assert offenders == [], f'mutations a Viewer could reach: {offenders}'
