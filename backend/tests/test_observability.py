"""Approved HTTP and collector-CLI seam checks, not private collaborator tests."""
from fastapi.testclient import TestClient
from app.main import create_app


def test_admin_observation_routes_require_authentication():
    with TestClient(create_app()) as client:
        for route in ['/api/v1/admin/metrics','/api/v1/admin/alerts','/api/v1/admin/logs/probe']:
            assert client.get(route).status_code == 401
