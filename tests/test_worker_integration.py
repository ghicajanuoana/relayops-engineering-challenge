"""One worker, real PostgreSQL, and the unchanged Mock Receiver over real HTTP."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

from fastapi.testclient import TestClient
import httpx
import psycopg
from psycopg.rows import dict_row
import pytest

from app import db
from app.main import app
from worker import worker


pytestmark = pytest.mark.skipif(
    not os.environ.get("RELAYOPS_TEST_DATABASE_URL"),
    reason="set RELAYOPS_TEST_DATABASE_URL to run worker integration tests",
)
ROOT = Path(__file__).resolve().parents[1]
SECRET = "worker-test-secret"
EVENT = {"event_id": "evt-worker", "type": "invoice.created", "payload": {"amount": 250, "unicode": "ț 東京", "nested": [None, True]}}


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def receiver_server():
    port = unused_port()
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "receiver.receiver:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, env={**os.environ, "WEBHOOK_SECRET": SECRET},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=1, trust_env=False) as client:
            deadline = time.monotonic() + 10
            while True:
                assert process.poll() is None, "Mock Receiver exited during startup"
                try:
                    if client.get("/health").status_code == 200:
                        break
                except httpx.RequestError:
                    pass
                assert time.monotonic() < deadline, "Mock Receiver startup timed out"
                time.sleep(0.05)
            yield client
    finally:
        process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()


@pytest.fixture
def delivery_env(database_url, receiver_server, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_URL", database_url)
    monkeypatch.setattr(worker, "WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(worker, "RECEIVER_URL", str(receiver_server.base_url.join("/webhook")))
    assert receiver_server.delete("/received").status_code == 200
    with psycopg.connect(database_url, autocommit=True, row_factory=dict_row) as conn:
        conn.execute("TRUNCATE delivery_attempts, events RESTART IDENTITY")
        with httpx.Client(timeout=5, trust_env=False) as client, TestClient(app) as api:
            assert api.post("/events", json=EVENT).status_code == 202
            yield conn, client, api, receiver_server


def assert_recorded(env, code, error_prefix=None):
    conn, client, api, receiver = env
    response = api.get("/events/" + EVENT["event_id"])
    assert response.status_code == 200
    event = response.json()
    assert event["status"] == ("delivered" if code is not None and 200 <= code < 300 else "failed")
    assert event["attempts"] == 1
    assert len(event["delivery_attempts"]) == 1
    attempt = event["delivery_attempts"][0]
    assert attempt["attempt"] == 1 and attempt["status_code"] == code
    assert attempt["created_at"]
    assert event["updated_at"] >= event["created_at"]
    if error_prefix:
        assert attempt["error"].startswith(error_prefix)
    else:
        assert attempt["error"] is None
    # No retry on the next poll, including for 429/5xx or a network failure.
    assert worker.process_pending(conn, client) == 0
    replay = api.post("/events", json=EVENT)
    assert replay.status_code == 200 and replay.json()["status"] == event["status"]
    return event


@pytest.mark.parametrize("code", [200, 201, 202, 299, 300, 302, 400, 401, 404, 429, 500, 503])
def test_http_outcomes_are_signed_and_recorded_once(delivery_env, code):
    conn, client, api, receiver = delivery_env
    receiver.post("/control", json={"fail_next": 1, "status_code": code, "retry_after": 60}).raise_for_status()
    assert worker.process_pending(conn, client) == 1
    assert_recorded(delivery_env, code)
    rows = receiver.get("/received").json()
    assert len(rows) == 1
    assert rows[0]["signature_valid"] is True
    assert rows[0]["body"] == EVENT
    assert rows[0]["status_code"] == code


def test_invalid_signature_is_failed(delivery_env, monkeypatch):
    conn, client, api, receiver = delivery_env
    monkeypatch.setattr(worker, "WEBHOOK_SECRET", "incorrect-test-secret")
    assert worker.process_pending(conn, client) == 1
    assert_recorded(delivery_env, 401)
    rows = receiver.get("/received").json()
    assert len(rows) == 1 and rows[0]["signature_valid"] is False


def test_connection_refused_is_recorded_without_retry(delivery_env, monkeypatch):
    conn, client, api, receiver = delivery_env
    monkeypatch.setattr(worker, "RECEIVER_URL", f"http://127.0.0.1:{unused_port()}/webhook")
    assert worker.process_pending(conn, client) == 1
    assert_recorded(delivery_env, None, "ConnectError:")
    assert receiver.get("/received").json() == []


def test_timeout_is_recorded_without_retry(delivery_env):
    conn, client, api, receiver = delivery_env
    receiver.post("/control", json={"delay_ms": 200, "delay_next": 1}).raise_for_status()
    with httpx.Client(timeout=0.05, trust_env=False) as short_timeout_client:
        assert worker.process_pending(conn, short_timeout_client) == 1
    assert_recorded(delivery_env, None, "ReadTimeout:")
    # Wait for the receiver's delayed response before resetting its state.
    deadline = time.monotonic() + 2
    while not receiver.get("/received").json():
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert len(receiver.get("/received").json()) == 1


def test_fetch_pending_respects_status_order_and_batch_size(delivery_env, monkeypatch):
    conn, client, api, receiver = delivery_env
    for index in range(3):
        assert api.post("/events", json={**EVENT, "event_id": f"evt-{index}"}).status_code == 202
    conn.execute("UPDATE events SET status='failed' WHERE event_id=%s", (EVENT["event_id"],))
    monkeypatch.setattr(worker, "BATCH_SIZE", 2)
    assert [row["event_id"] for row in worker.fetch_pending(conn)] == ["evt-0", "evt-1"]


def test_attempt_and_event_update_roll_back_together(delivery_env):
    conn, client, api, receiver = delivery_env
    conn.execute("""
        CREATE FUNCTION reject_event_update() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'test update failure'; END;
        $$;
        CREATE TRIGGER reject_event_update BEFORE UPDATE ON events
        FOR EACH ROW EXECUTE FUNCTION reject_event_update();
    """)
    try:
        with pytest.raises(psycopg.errors.RaiseException, match="test update failure"):
            worker.process_pending(conn, client)
        event = api.get("/events/" + EVENT["event_id"]).json()
        assert event["status"] == "pending" and event["attempts"] == 0
        assert event["delivery_attempts"] == []
    finally:
        conn.execute("DROP TRIGGER reject_event_update ON events; DROP FUNCTION reject_event_update()")


@pytest.mark.parametrize("code", [200, 503])
def test_one_worker_process_delivers_once(delivery_env, database_url, code):
    conn, client, api, receiver = delivery_env
    receiver.post("/control", json={"fail_next": 1, "status_code": code}).raise_for_status()
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "worker" / "worker.py")],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": database_url, "RECEIVER_URL": worker.RECEIVER_URL,
             "WEBHOOK_SECRET": SECRET, "POLL_INTERVAL": "0.05"},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while api.get("/events/" + EVENT["event_id"]).json()["status"] == "pending":
            assert process.poll() is None, "worker exited unexpectedly"
            assert time.monotonic() < deadline, "worker delivery timed out"
            time.sleep(0.05)
        time.sleep(0.15)
        event = api.get("/events/" + EVENT["event_id"]).json()
        assert event["status"] == ("delivered" if code == 200 else "failed")
        assert event["attempts"] == 1
        assert event["delivery_attempts"][0]["status_code"] == code
        rows = receiver.get("/received").json()
        assert len(rows) == 1 and rows[0]["signature_valid"] is True
    finally:
        process.terminate()
        try:
            output, _ = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            output, _ = process.communicate()
        print(output)
