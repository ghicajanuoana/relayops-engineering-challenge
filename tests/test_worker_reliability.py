"""Real worker processes testing PostgreSQL ownership, crashes, and SIGTERM."""
from collections import Counter
import os
import subprocess
import sys
import time
from uuid import uuid4

import psycopg
import pytest

from test_worker_integration import EVENT, ROOT, SECRET, delivery_env, receiver_server
from worker import worker


pytestmark = pytest.mark.skipif(
    not os.environ.get("RELAYOPS_TEST_DATABASE_URL"),
    reason="set RELAYOPS_TEST_DATABASE_URL to run worker reliability tests",
)


def wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition did not become true"
        time.sleep(0.02)


@pytest.fixture
def start_worker(delivery_env, database_url):
    processes = []

    def start(app_name=None, poll_interval="0.05"):
        name = app_name or "reliability-" + uuid4().hex
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "worker" / "worker.py")], cwd=ROOT,
            env={**os.environ, "DATABASE_URL": database_url, "RECEIVER_URL": worker.RECEIVER_URL,
                 "WEBHOOK_SECRET": SECRET, "POLL_INTERVAL": poll_interval, "PGAPPNAME": name},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        process.app_name = name
        processes.append(process)
        return process

    yield start
    for process in processes:
        if process.poll() is None:
            process.terminate()
        try:
            output, _ = process.communicate(timeout=7)
        except subprocess.TimeoutExpired:
            process.kill()
            output, _ = process.communicate()
        print(output)


def event_is_locked(conn):
    try:
        with conn.transaction():
            conn.execute("SELECT id FROM events WHERE event_id=%s FOR UPDATE NOWAIT", (EVENT["event_id"],))
    except psycopg.errors.LockNotAvailable:
        return True
    return False


def connected(conn, process):
    return conn.execute(
        "SELECT count(*) AS n FROM pg_stat_activity WHERE application_name=%s",
        (process.app_name,),
    ).fetchone()["n"] == 1


def test_two_workers_deliver_one_event_only_once(delivery_env, start_worker):
    conn, client, api, receiver = delivery_env
    receiver.post("/control", json={"delay_ms": 500}).raise_for_status()
    workers = [start_worker(), start_worker()]
    wait_for(lambda: api.get("/events/" + EVENT["event_id"]).json()["status"] == "delivered")
    time.sleep(0.7)
    rows = receiver.get("/received").json()
    assert len(rows) == 1
    assert rows[0]["signature_valid"] is True
    assert all(process.poll() is None for process in workers)
    event = api.get("/events/" + EVENT["event_id"]).json()
    assert event["attempts"] == 1 and len(event["delivery_attempts"]) == 1


def test_two_workers_share_queue_without_duplicates(delivery_env, start_worker):
    conn, client, api, receiver = delivery_env
    ids = [EVENT["event_id"]] + ["parallel-" + str(i) for i in range(19)]
    for event_id in ids[1:]:
        assert api.post("/events", json={**EVENT, "event_id": event_id}).status_code == 202
    receiver.post("/control", json={"delay_ms": 50}).raise_for_status()
    workers = [start_worker(), start_worker()]
    wait_for(lambda: conn.execute("SELECT count(*) AS n FROM events WHERE status='delivered'").fetchone()["n"] == len(ids))
    counts = Counter(row["event_id"] for row in receiver.get("/received").json())
    assert counts == Counter({event_id: 1 for event_id in ids})
    assert conn.execute("SELECT count(*) AS n FROM delivery_attempts").fetchone()["n"] == len(ids)
    assert all(process.poll() is None for process in workers)


def test_two_workers_preserve_retry_budget_and_delays(delivery_env, start_worker):
    conn, client, api, receiver = delivery_env
    receiver.post("/control", json={"fail_next": 2, "status_code": 503, "retry_after": 1}).raise_for_status()
    workers = [start_worker(), start_worker()]
    wait_for(lambda: api.get("/events/" + EVENT["event_id"]).json()["status"] == "delivered")
    event = api.get("/events/" + EVENT["event_id"]).json()
    assert event["attempts"] == 3
    assert [a["status_code"] for a in event["delivery_attempts"]] == [503, 503, 200]
    rows = receiver.get("/received").json()
    assert len(rows) == 3 and all(row["signature_valid"] for row in rows)
    assert rows[1]["arrived_at"] - rows[0]["answered_at"] >= 1
    assert rows[2]["arrived_at"] - rows[1]["answered_at"] >= 2
    assert all(process.poll() is None for process in workers)


def test_locked_event_does_not_block_other_work(delivery_env, start_worker):
    conn, client, api, receiver = delivery_env
    assert api.post("/events", json={**EVENT, "event_id": "unlocked"}).status_code == 202
    with conn.transaction():
        conn.execute("SELECT id FROM events WHERE event_id=%s FOR UPDATE", (EVENT["event_id"],))
        start_worker()
        start_worker()
        wait_for(lambda: api.get("/events/unlocked").json()["status"] == "delivered")
        assert receiver.get("/received").json()[0]["event_id"] == "unlocked"
        assert api.get("/events/" + EVENT["event_id"]).json()["attempts"] == 0
    wait_for(lambda: api.get("/events/" + EVENT["event_id"]).json()["status"] == "delivered")
    assert Counter(row["event_id"] for row in receiver.get("/received").json()) == {EVENT["event_id"]: 1, "unlocked": 1}


def test_killed_worker_releases_unfinished_event(delivery_env, start_worker):
    conn, client, api, receiver = delivery_env
    receiver.post("/control", json={"delay_ms": 1500, "delay_next": 1}).raise_for_status()
    owner = start_worker()
    wait_for(lambda: event_is_locked(conn))
    owner.kill()
    owner.wait(timeout=5)
    assert api.get("/events/" + EVENT["event_id"]).json()["attempts"] == 0
    survivor = start_worker()
    wait_for(lambda: api.get("/events/" + EVENT["event_id"]).json()["status"] == "delivered")
    # A request already sent by the killed process may still finish downstream.
    time.sleep(1.6)
    rows = receiver.get("/received").json()
    assert len(rows) >= 1 and all(row["signature_valid"] for row in rows)
    event = api.get("/events/" + EVENT["event_id"]).json()
    assert event["attempts"] == 1 and len(event["delivery_attempts"]) == 1
    assert survivor.poll() is None


def test_crash_after_remote_success_can_duplicate_without_losing_event(delivery_env, start_worker):
    conn, client, api, receiver = delivery_env
    owner_name = "crash-owner-" + uuid4().hex
    # Pause only the owner's database record after the receiver has answered 200.
    conn.execute(f"""
        CREATE FUNCTION pause_owner_record() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF current_setting('application_name') = '{owner_name}' THEN
                PERFORM pg_sleep(3);
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER pause_owner_record BEFORE INSERT ON delivery_attempts
        FOR EACH ROW EXECUTE FUNCTION pause_owner_record();
    """)
    try:
        owner = start_worker(app_name=owner_name)
        wait_for(lambda: conn.execute(
            "SELECT count(*) AS n FROM pg_stat_activity WHERE application_name=%s AND wait_event='PgSleep'",
            (owner_name,),
        ).fetchone()["n"] == 1)
        assert len(receiver.get("/received").json()) == 1
        survivor = start_worker()
        wait_for(lambda: connected(conn, survivor))
        owner.kill()
        owner.wait(timeout=5)
        wait_for(lambda: api.get("/events/" + EVENT["event_id"]).json()["status"] == "delivered", timeout=15)
        rows = receiver.get("/received").json()
        assert len(rows) == 2 and all(row["status_code"] == 200 for row in rows)
        event = api.get("/events/" + EVENT["event_id"]).json()
        assert event["attempts"] == 1 and len(event["delivery_attempts"]) == 1
        assert survivor.poll() is None
    finally:
        conn.execute("DROP TRIGGER pause_owner_record ON delivery_attempts; DROP FUNCTION pause_owner_record()")


def test_sigterm_finishes_current_event_without_claiming_next(delivery_env, start_worker):
    conn, client, api, receiver = delivery_env
    assert api.post("/events", json={**EVENT, "event_id": "after-shutdown"}).status_code == 202
    receiver.post("/control", json={"delay_ms": 500, "delay_next": 1}).raise_for_status()
    process = start_worker()
    wait_for(lambda: event_is_locked(conn))
    started = time.monotonic()
    process.terminate()
    assert process.wait(timeout=6) == 0
    assert time.monotonic() - started < 6
    event = api.get("/events/" + EVENT["event_id"]).json()
    assert event["status"] == "delivered" and event["attempts"] == 1
    other = api.get("/events/after-shutdown").json()
    assert other["status"] == "pending" and other["attempts"] == 0
    assert len(receiver.get("/received").json()) == 1
    assert not event_is_locked(conn)
    start_worker()
    wait_for(lambda: api.get("/events/after-shutdown").json()["status"] == "delivered")


def test_sigterm_interrupts_idle_poll_wait(delivery_env, start_worker):
    conn, client, api, receiver = delivery_env
    conn.execute("UPDATE events SET status='failed'")
    process = start_worker(poll_interval="60")
    wait_for(lambda: connected(conn, process))
    started = time.monotonic()
    process.terminate()
    assert process.wait(timeout=2) == 0
    assert time.monotonic() - started < 2


def test_sigterm_finishes_bounded_timeout_and_saves_retry(delivery_env, start_worker):
    conn, client, api, receiver = delivery_env
    receiver.post("/control", json={"delay_ms": 5500, "delay_next": 1}).raise_for_status()
    process = start_worker()
    wait_for(lambda: event_is_locked(conn))
    started = time.monotonic()
    process.terminate()
    assert process.wait(timeout=6) == 0
    assert time.monotonic() - started < 6
    event = api.get("/events/" + EVENT["event_id"]).json()
    assert event["status"] == "pending" and event["attempts"] == 1
    assert event["delivery_attempts"][0]["status_code"] is None
    assert event["delivery_attempts"][0]["error"]
    assert conn.execute("SELECT next_attempt_at FROM events").fetchone()["next_attempt_at"] is not None
    assert not event_is_locked(conn)
    wait_for(lambda: len(receiver.get("/received").json()) == 1, timeout=2)
