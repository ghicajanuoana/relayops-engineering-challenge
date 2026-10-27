"""Real PostgreSQL tests; set RELAYOPS_TEST_DATABASE_URL to an admin database.

Each run creates and drops its own database. The application database is untouched.
"""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app


pytestmark = pytest.mark.skipif(
    not os.environ.get("RELAYOPS_TEST_DATABASE_URL"),
    reason="set RELAYOPS_TEST_DATABASE_URL to run real PostgreSQL tests",
)


@pytest.fixture
def client(monkeypatch, database_url):
    monkeypatch.setattr(db, "DATABASE_URL", database_url)
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("TRUNCATE delivery_attempts, events RESTART IDENTITY")
    with TestClient(app) as client:
        yield client


def event(**changes):
    return {"event_id": "evt-test", "type": "invoice.created", "payload": {"amount": 250}, **changes}


def test_create_and_get(client):
    body = event()
    response = client.post("/events", json=body)
    assert response.status_code == 202
    assert response.json() == {"event_id": body["event_id"], "status": "pending"}
    response = client.get("/events/evt-test")
    assert response.status_code == 200
    assert response.json().keys() == {
        "event_id", "type", "payload", "status", "attempts", "delivery_attempts", "created_at", "updated_at"
    }
    row = response.json()
    assert {key: row[key] for key in body} == body
    assert row["status"] == "pending" and row["attempts"] == 0
    assert row["delivery_attempts"] == []
    assert datetime.fromisoformat(row["created_at"]) == datetime.fromisoformat(row["updated_at"])


def test_unknown_event(client):
    assert client.get("/events/unknown").status_code == 404


def test_requests_release_connections_on_success_and_error(client, database_url):
    assert client.post("/events", json=event()).status_code == 202
    assert client.post("/events", json=event(type="other")).status_code == 409
    assert client.get("/events/evt-test").status_code == 200
    assert client.get("/events/unknown").status_code == 404
    with psycopg.connect(database_url, autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()"
        ).fetchone()[0] == 0


def test_duplicate_returns_current_state_without_mutation(client, database_url):
    body = event(payload={"nested": {"a": 1, "b": 2}, "items": [1, 2]})
    assert client.post("/events", json=body).status_code == 202
    with psycopg.connect(database_url) as conn:
        conn.execute("UPDATE events SET status='delivered', attempts=1")
    before = client.get("/events/evt-test").json()
    duplicate = event(payload={"items": [1, 2], "nested": {"b": 2, "a": 1}})
    response = client.post("/events", json=duplicate)
    assert response.status_code == 200
    assert response.json() == {"event_id": "evt-test", "status": "delivered"}
    assert client.get("/events/evt-test").json() == before
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1


@pytest.mark.parametrize("changes", [
    {"type": "invoice.updated"}, {"payload": {"amount": 251}},
    {"payload": {"amount": True}},
])
def test_conflict_preserves_original(client, changes):
    assert client.post("/events", json=event()).status_code == 202
    before = client.get("/events/evt-test").json()
    assert client.post("/events", json=event(**changes)).status_code == 409
    assert client.get("/events/evt-test").json() == before
    assert client.post("/events", json=event(event_id="after-conflict")).status_code == 202


def test_jsonb_content_comparison(client):
    body = event(payload={"value": 1, "items": [1, 2]})
    assert client.post("/events", json=body).status_code == 202
    assert client.post("/events", json=event(payload={"value": 1.0, "items": [1, 2]})).status_code == 200
    assert client.post("/events", json=event(payload={"value": True, "items": [1, 2]})).status_code == 409
    assert client.post("/events", json=event(payload={"value": 1, "items": [2, 1]})).status_code == 409


def test_history_order_nulls_and_timestamps(client, database_url):
    assert client.post("/events", json=event()).status_code == 202
    with psycopg.connect(database_url) as conn:
        pk = conn.execute("SELECT id FROM events").fetchone()[0]
        conn.execute("UPDATE events SET status='delivered', attempts=3")
        for attempt, code, error in [(3, 200, None), (1, 503, None), (2, None, "connection timeout")]:
            conn.execute(
                "INSERT INTO delivery_attempts(event_pk, attempt, status_code, error) VALUES (%s,%s,%s,%s)",
                (pk, attempt, code, error),
            )
    result = client.get("/events/evt-test").json()
    assert result["status"] == "delivered" and result["attempts"] == 3
    history = result["delivery_attempts"]
    assert [row["attempt"] for row in history] == [1, 2, 3]
    assert [row["status_code"] for row in history] == [503, None, 200]
    assert [row["error"] for row in history] == [None, "connection timeout", None]
    for row in history:
        assert row.keys() == {"attempt", "status_code", "error", "created_at"}
        assert datetime.fromisoformat(row["created_at"]).tzinfo is not None


@pytest.mark.parametrize("different_content", [False, True])
def test_concurrent_same_id(client, database_url, different_content):
    count = 12
    barrier = Barrier(count)
    bodies = [event(payload={"value": i if different_content else 1}) for i in range(count)]

    def submit(body):
        barrier.wait(timeout=10)
        return client.post("/events", json=body)

    with ThreadPoolExecutor(max_workers=count) as pool:
        results = list(pool.map(submit, bodies))
    codes = [response.status_code for response in results]
    assert codes.count(202) == 1
    assert codes.count(409 if different_content else 200) == count - 1
    winner = bodies[codes.index(202)]
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1
        assert conn.execute("SELECT payload FROM events").fetchone()[0] == winner["payload"]


@pytest.mark.parametrize("column,value", [
    ("event_id", ""), ("event_id", "x" * 201), ("type", ""),
    ("payload", Jsonb([])), ("status", "unknown"), ("attempts", -1), ("attempts", 4),
])
def test_database_rejects_invalid_event(client, database_url, column, value):
    assert client.post("/events", json=event()).status_code == 202
    with pytest.raises(psycopg.errors.CheckViolation):
        with psycopg.connect(database_url) as conn:
            conn.execute(sql.SQL("UPDATE events SET {}=%s").format(sql.Identifier(column)), (value,))
    assert client.get("/events/evt-test").json()["status"] == "pending"


def test_database_uniqueness_and_rollback(client, database_url):
    assert client.post("/events", json=event()).status_code == 202
    with pytest.raises(psycopg.errors.UniqueViolation):
        with psycopg.connect(database_url) as conn:
            conn.execute("INSERT INTO events(event_id,type,payload) VALUES ('rolled-back','test','{}')")
            conn.execute("INSERT INTO events(event_id,type,payload) VALUES ('evt-test','test','{}')")
    assert client.get("/events/rolled-back").status_code == 404
    assert client.post("/events", json=event(event_id="after-rollback")).status_code == 202


def test_database_attempt_constraints(client, database_url):
    assert client.post("/events", json=event()).status_code == 202
    with psycopg.connect(database_url) as conn:
        pk = conn.execute("SELECT id FROM events").fetchone()[0]
        conn.execute("INSERT INTO delivery_attempts(event_pk,attempt,status_code) VALUES (%s,1,200)", (pk,))
    with pytest.raises(psycopg.errors.UniqueViolation):
        with psycopg.connect(database_url) as conn:
            conn.execute("INSERT INTO delivery_attempts(event_pk,attempt) VALUES (%s,1)", (pk,))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with psycopg.connect(database_url) as conn:
            conn.execute("INSERT INTO delivery_attempts(event_pk,attempt) VALUES (%s,1)", (pk+1000,))
    for attempt, code in [(0, 200), (4, 200), (2, 999)]:
        with pytest.raises(psycopg.errors.CheckViolation):
            with psycopg.connect(database_url) as conn:
                conn.execute("INSERT INTO delivery_attempts(event_pk,attempt,status_code) VALUES (%s,%s,%s)", (pk, attempt, code))
