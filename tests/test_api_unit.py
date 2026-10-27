import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.mark.parametrize("raw", [
    "{}", "null", "[]", "{",
    '{"event_id":"evt","type":"test","payload":[]}',
    '{"event_id":"evt","type":"test","payload":{"value":NaN}}',
    '{"event_id":"evt","type":"test","payload":{"value":Infinity}}',
    '{"event_id":"evt","type":"test","payload":{"value":"\\u0000"}}',
    '{"event_id":"evt","type":"test","payload":{"value":"\\ud800"}}',
    '{"event_id":"evt","type":"test","payload":{"\\ud800":1}}',
    '{"event_id":"evt","type":"test","payload":{"nested":["\\udfff"]}}',
])
def test_invalid_request_never_opens_database(monkeypatch, raw):
    def unexpected_connection():
        pytest.fail("invalid input must not access PostgreSQL")

    monkeypatch.setattr("app.main.get_conn", unexpected_connection)
    with TestClient(app) as client:
        response = client.post("/events", content=raw, headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)
