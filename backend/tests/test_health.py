from fastapi.testclient import TestClient
from app.main import create_app


def test_health_is_live_without_database_connection():
    with TestClient(create_app()) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_unknown_route_has_structured_error_and_request_id():
    with TestClient(create_app()) as client:
        response = client.get("/does-not-exist", headers={"X-Request-ID": "test-request-123"})
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"
    assert response.json()["request_id"] == "test-request-123"
    assert response.headers["X-Request-ID"] == "test-request-123"
