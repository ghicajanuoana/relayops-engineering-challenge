import json
import logging
import re

from fastapi.testclient import TestClient
import pytest

from app.main import app


@pytest.mark.parametrize("request_id", [None, "client-trace-unit"])
@pytest.mark.parametrize("method,path,body,status", [
    ("GET", "/health", None, 200),
    ("GET", "/missing", None, 404),
    ("POST", "/events", {}, 422),
    ("POST", "/health", {}, 405),
])
def test_api_traces_success_and_errors(caplog, request_id, method, path, body, status):
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    headers = {"X-Request-ID": request_id} if request_id else {}
    with TestClient(app) as client:
        response = client.request(method, path, json=body, headers=headers)
    assert response.status_code == status
    returned_id = response.headers["X-Request-ID"]
    assert response.headers.get_list("X-Request-ID") == [returned_id]
    if request_id:
        assert returned_id == request_id
    else:
        assert re.fullmatch(r"[0-9a-f]{32}", returned_id)
    records = [json.loads(record.message) for record in caplog.records if record.name == "uvicorn.error"]
    assert {"request_id": returned_id, "method": method, "path": path, "status": status} in records


@pytest.mark.parametrize("request_id", [None, "client-trace-error"])
def test_unhandled_error_has_request_id_and_log(monkeypatch, caplog, request_id):
    caplog.set_level(logging.INFO, logger="uvicorn.error")

    def unavailable_database():
        raise RuntimeError("test database unavailable")

    monkeypatch.setattr("app.main.get_conn", unavailable_database)
    headers = {"X-Request-ID": request_id} if request_id else {}
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/metrics", headers=headers)
    assert response.status_code == 500
    returned_id = response.headers["X-Request-ID"]
    assert returned_id == request_id if request_id else re.fullmatch(r"[0-9a-f]{32}", returned_id)
    assert response.json() == {"detail": "Internal Server Error"}
    assert any(returned_id in record.message and '"status": 500' in record.message for record in caplog.records)


def test_health_keeps_existing_liveness_contract_without_database(monkeypatch):
    def unexpected_database_access():
        pytest.fail("liveness must not open a database connection")

    monkeypatch.setattr("app.main.get_conn", unexpected_database_access)
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_generated_api_request_ids_are_distinct():
    with TestClient(app) as client:
        first = client.get("/health").headers["X-Request-ID"]
        second = client.get("/health").headers["X-Request-ID"]
    assert first != second
