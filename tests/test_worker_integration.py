"""One worker, real PostgreSQL, and the unchanged Mock Receiver over real HTTP."""
import asyncio
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


def run_pending(conn, timeout=5):
    async def run():
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            return await worker.process_pending(conn, client)

    return asyncio.run(run())


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
    expected = "delivered" if code is not None and 200 <= code < 300 else (
        "pending" if code is None or code == 429 or 500 <= code < 600 else "failed"
    )
    assert event["status"] == expected
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
    # A scheduled retry must not run immediately on the next poll.
    assert run_pending(conn) == 0
    replay = api.post("/events", json=EVENT)
    assert replay.status_code == 200 and replay.json()["status"] == event["status"]
    return event


@pytest.mark.parametrize("code", [200, 201, 202, 299, 300, 302, 400, 401, 404, 429, 500, 503])
def test_http_outcomes_are_signed_and_recorded_once(delivery_env, code):
    conn, client, api, receiver = delivery_env
    receiver.post("/control", json={"fail_next": 1, "status_code": code, "retry_after": 60}).raise_for_status()
    assert run_pending(conn) == 1
    assert_recorded(delivery_env, code)
    rows = receiver.get("/received").json()
    assert len(rows) == 1
    assert rows[0]["signature_valid"] is True
    assert rows[0]["body"] == EVENT
    assert rows[0]["status_code"] == code


def test_invalid_signature_is_failed(delivery_env, monkeypatch):
    conn, client, api, receiver = delivery_env
    monkeypatch.setattr(worker, "WEBHOOK_SECRET", "incorrect-test-secret")
    assert run_pending(conn) == 1
    assert_recorded(delivery_env, 401)
    rows = receiver.get("/received").json()
    assert len(rows) == 1 and rows[0]["signature_valid"] is False


def test_connection_refused_is_recorded_and_scheduled(delivery_env, monkeypatch):
    conn, client, api, receiver = delivery_env
    monkeypatch.setattr(worker, "RECEIVER_URL", f"http://127.0.0.1:{unused_port()}/webhook")
    assert run_pending(conn) == 1
    assert_recorded(delivery_env, None, "ConnectError:")
    assert receiver.get("/received").json() == []


def test_timeout_is_recorded_and_scheduled(delivery_env):
    conn, client, api, receiver = delivery_env
    receiver.post("/control", json={"delay_ms": 200, "delay_next": 1}).raise_for_status()
    assert run_pending(conn, timeout=0.05) == 1
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
            run_pending(conn)
        event = api.get("/events/" + EVENT["event_id"]).json()
        assert event["status"] == "pending" and event["attempts"] == 0
        assert event["delivery_attempts"] == []
    finally:
        conn.execute("DROP TRIGGER reject_event_update ON events; DROP FUNCTION reject_event_update()")


@pytest.mark.parametrize("code", [200, 400])
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


def schedule(conn):
    return conn.execute(
        "SELECT attempts, status, next_attempt_at, clock_timestamp() AS db_now FROM events WHERE event_id=%s",
        (EVENT["event_id"],),
    ).fetchone()


def wait_until_due(conn, row):
    delay = (row["next_attempt_at"] - row["db_now"]).total_seconds()
    time.sleep(max(0, delay) + 0.03)


@pytest.mark.parametrize("code", [429, 500, 503])
@pytest.mark.parametrize("failures", [1, 2])
def test_temporary_failure_then_success(delivery_env, code, failures):
    conn, client, api, receiver = delivery_env
    receiver.post("/control", json={"fail_next": failures, "status_code": code}).raise_for_status()
    for attempt in range(1, failures + 1):
        assert run_pending(conn) == 1
        row = schedule(conn)
        assert row["status"] == "pending" and row["attempts"] == attempt
        assert row["next_attempt_at"] > row["db_now"]
        assert run_pending(conn) == 0
        wait_until_due(conn, schedule(conn))
    assert run_pending(conn) == 1
    result = api.get("/events/" + EVENT["event_id"]).json()
    assert result["status"] == "delivered" and result["attempts"] == failures + 1
    assert [a["status_code"] for a in result["delivery_attempts"]] == [code] * failures + [200]
    assert schedule(conn)["next_attempt_at"] is None
    assert len(receiver.get("/received").json()) == failures + 1


@pytest.mark.parametrize("code", [503, None])
def test_three_attempt_limit_and_exponential_backoff(delivery_env, monkeypatch, code):
    conn, client, api, receiver = delivery_env
    if code is None:
        monkeypatch.setattr(worker, "RECEIVER_URL", f"http://127.0.0.1:{unused_port()}/webhook")
    else:
        receiver.post("/control", json={"fail_next": 5, "status_code": code}).raise_for_status()
    for attempt in range(1, 4):
        started = conn.execute("SELECT clock_timestamp() AS at").fetchone()["at"]
        assert run_pending(conn) == 1
        row = schedule(conn)
        assert row["attempts"] == attempt
        assert run_pending(conn) == 0
        if attempt < 3:
            assert row["status"] == "pending"
            assert (row["next_attempt_at"] - started).total_seconds() >= 2 ** (attempt - 1)
            wait_until_due(conn, schedule(conn))
        else:
            assert row["status"] == "failed" and row["next_attempt_at"] is None
    result = api.get("/events/" + EVENT["event_id"]).json()
    assert [a["attempt"] for a in result["delivery_attempts"]] == [1, 2, 3]
    assert [a["status_code"] for a in result["delivery_attempts"]] == [code] * 3
    assert len(receiver.get("/received").json()) == (0 if code is None else 3)


def test_timeout_then_success(delivery_env):
    conn, client, api, receiver = delivery_env
    receiver.post("/control", json={"delay_ms": 200, "delay_next": 1}).raise_for_status()
    assert run_pending(conn, timeout=0.05) == 1
    assert_recorded(delivery_env, None, "ReadTimeout:")
    wait_until_due(conn, schedule(conn))
    assert run_pending(conn) == 1
    result = api.get("/events/" + EVENT["event_id"]).json()
    assert result["status"] == "delivered" and result["attempts"] == 2
    assert [a["status_code"] for a in result["delivery_attempts"]] == [None, 200]
    assert len(receiver.get("/received").json()) == 2


def test_retry_after_does_not_block_other_events(delivery_env):
    conn, client, api, receiver = delivery_env
    receiver.post("/control", json={"fail_next": 1, "status_code": 429, "retry_after": 2}).raise_for_status()
    assert run_pending(conn) == 1
    first = schedule(conn)
    before_due = first["next_attempt_at"]
    other = {**EVENT, "event_id": "evt-other"}
    assert api.post("/events", json=other).status_code == 202
    assert run_pending(conn) == 1
    assert api.get("/events/evt-other").json()["status"] == "delivered"
    assert api.get("/events/" + EVENT["event_id"]).json()["attempts"] == 1
    assert conn.execute("SELECT clock_timestamp() AS at").fetchone()["at"] < before_due
    assert run_pending(conn) == 0
    wait_until_due(conn, schedule(conn))
    assert run_pending(conn) == 1
    rows = receiver.get("/received").json()
    assert [r["event_id"] for r in rows] == [EVENT["event_id"], "evt-other", EVENT["event_id"]]
    assert rows[2]["arrived_at"] >= before_due.timestamp()
    assert rows[2]["arrived_at"] - rows[0]["answered_at"] >= 2


def test_network_retry_uses_persisted_attempt_count(delivery_env, monkeypatch):
    conn, client, api, receiver = delivery_env
    destination = worker.RECEIVER_URL
    monkeypatch.setattr(worker, "RECEIVER_URL", f"http://127.0.0.1:{unused_port()}/webhook")
    assert run_pending(conn) == 1
    assert_recorded(delivery_env, None, "ConnectError:")
    monkeypatch.setattr(worker, "RECEIVER_URL", destination)
    wait_until_due(conn, schedule(conn))
    assert run_pending(conn) == 1
    result = api.get("/events/" + EVENT["event_id"]).json()
    assert result["status"] == "delivered" and result["attempts"] == 2
    assert [r["status_code"] for r in result["delivery_attempts"]] == [None, 200]


def test_total_http_attempt_has_five_second_deadline(delivery_env):
    conn, client, api, receiver = delivery_env
    receiver.post("/control", json={"delay_ms": 5500, "delay_next": 1}).raise_for_status()
    started = time.monotonic()
    # Disable HTTPX's phase timeouts: the worker's total deadline must still fire.
    assert run_pending(conn, timeout=None) == 1
    elapsed = time.monotonic() - started
    assert 4.5 <= elapsed < 5.8
    result = assert_recorded(delivery_env, None, "TimeoutError:")
    assert "5 seconds" in result["delivery_attempts"][0]["error"]
    deadline = time.monotonic() + 2
    while not receiver.get("/received").json():
        assert time.monotonic() < deadline
        time.sleep(0.02)


def test_scheduled_retry_survives_worker_restart(delivery_env, database_url):
    conn, client, api, receiver = delivery_env
    receiver.post("/control", json={"fail_next": 1, "status_code": 503, "retry_after": 3}).raise_for_status()

    def start_worker():
        return subprocess.Popen(
            [sys.executable, str(ROOT / "worker" / "worker.py")], cwd=ROOT,
            env={**os.environ, "DATABASE_URL": database_url, "RECEIVER_URL": worker.RECEIVER_URL,
                 "WEBHOOK_SECRET": SECRET, "POLL_INTERVAL": "0.05"},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )

    def stop_worker(process):
        process.terminate()
        try:
            output, _ = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            output, _ = process.communicate()
        print(output)

    process = start_worker()
    try:
        deadline = time.monotonic() + 5
        while schedule(conn)["attempts"] == 0:
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.02)
        saved = schedule(conn)
        assert saved["status"] == "pending" and saved["attempts"] == 1
    finally:
        stop_worker(process)
    assert schedule(conn)["next_attempt_at"] == saved["next_attempt_at"]
    process = start_worker()
    try:
        deadline = time.monotonic() + 6
        while api.get("/events/" + EVENT["event_id"]).json()["status"] != "delivered":
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.02)
        result = api.get("/events/" + EVENT["event_id"]).json()
        assert result["attempts"] == 2
        assert [a["status_code"] for a in result["delivery_attempts"]] == [503, 200]
        rows = receiver.get("/received").json()
        assert len(rows) == 2
        assert rows[1]["arrived_at"] >= saved["next_attempt_at"].timestamp()
        assert rows[1]["arrived_at"] - rows[0]["answered_at"] >= 3
    finally:
        stop_worker(process)


def test_retry_migration_preserves_existing_event_and_history(delivery_env):
    conn, client, api, receiver = delivery_env
    conn.execute("UPDATE events SET status='failed', attempts=1 WHERE event_id=%s", (EVENT["event_id"],))
    conn.execute("""
        INSERT INTO delivery_attempts(event_pk, attempt, status_code)
        SELECT id, 1, 503 FROM events WHERE event_id=%s
    """, (EVENT["event_id"],))
    # Reconstruct the pre-Step-5 schema in this isolated test database only.
    conn.execute("ALTER TABLE events DROP COLUMN next_attempt_at")
    before_event = conn.execute("SELECT * FROM events").fetchall()
    before_history = conn.execute("SELECT * FROM delivery_attempts").fetchall()
    with conn.transaction():
        conn.execute((ROOT / "db" / "003_retry_schedule.sql").read_text(encoding="utf-8"))
    after_event = conn.execute("SELECT * FROM events").fetchall()
    assert [{k: v for k, v in row.items() if k != "next_attempt_at"} for row in after_event] == before_event
    assert after_event[0]["next_attempt_at"] is None
    assert conn.execute("SELECT * FROM delivery_attempts").fetchall() == before_history
