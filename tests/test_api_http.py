"""Live HTTP contract tests; set RELAYOPS_BASE_URL and DATABASE_URL.

Run with workers stopped. Only rows belonging to these tests are removed.
"""
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import httpx
import psycopg
import pytest


pytestmark = pytest.mark.skipif(
    not os.environ.get("RELAYOPS_BASE_URL"),
    reason="set RELAYOPS_BASE_URL to run live HTTP API tests",
)


@pytest.fixture
def live_event():
    body = {"event_id": "api-http-" + uuid4().hex, "type": "invoice.created", "payload": {"amount": 250}}
    yield body
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        conn.execute(
            "DELETE FROM delivery_attempts WHERE event_pk IN (SELECT id FROM events WHERE event_id=%s)",
            (body["event_id"],),
        )
        conn.execute("DELETE FROM events WHERE event_id=%s", (body["event_id"],))


@pytest.fixture
def live_client():
    with httpx.Client(base_url=os.environ["RELAYOPS_BASE_URL"], timeout=10) as client:
        yield client


def test_http_contract(live_client, live_event):
    response = live_client.post("/events", json=live_event)
    assert response.status_code == 202
    assert response.json() == {"event_id": live_event["event_id"], "status": "pending"}
    response = live_client.get("/events/" + live_event["event_id"])
    assert response.status_code == 200
    original = response.json()
    assert original["payload"] == live_event["payload"]
    assert original["delivery_attempts"] == []
    response = live_client.post("/events", json=live_event)
    assert response.status_code == 200
    assert response.json() == {"event_id": live_event["event_id"], "status": "pending"}
    assert live_client.post("/events", json={**live_event, "type": "other"}).status_code == 409
    assert live_client.get("/events/" + live_event["event_id"]).json() == original
    assert live_client.post("/events", json={"event_id": live_event["event_id"]}).status_code == 422
    assert live_client.get("/events/missing-" + uuid4().hex).status_code == 404


@pytest.mark.parametrize("different_content", [False, True])
def test_http_concurrent_same_id(live_client, live_event, different_content):
    count = 12
    barrier = Barrier(count)

    def submit(index):
        barrier.wait(timeout=10)
        body = {**live_event, "payload": {"value": index if different_content else 1}}
        return live_client.post("/events", json=body).status_code

    with ThreadPoolExecutor(max_workers=count) as pool:
        codes = list(pool.map(submit, range(count)))
    assert codes.count(202) == 1
    assert codes.count(409 if different_content else 200) == count - 1
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        assert conn.execute("SELECT count(*) FROM events WHERE event_id=%s", (live_event["event_id"],)).fetchone()[0] == 1


def test_http_large_payload(live_client, live_event):
    body = {**live_event, "payload": {"data": "x" * (1048576 - 11)}}
    assert live_client.post("/events", json=body).status_code == 202
    assert live_client.get("/events/" + live_event["event_id"]).json()["payload"] == body["payload"]


def test_http_valid_unicode_pair_and_literal_escape(live_client, live_event):
    raw = (
        '{"event_id":"' + live_event["event_id"] + '","type":"test",'
        '"payload":{"emoji":"\\ud83d\\ude00","literal":"\\\\ud800"}}'
    )
    response = live_client.post("/events", content=raw, headers={"Content-Type": "application/json"})
    assert response.status_code == 202
    saved = live_client.get("/events/" + live_event["event_id"]).json()
    assert saved["payload"] == {"emoji": "😀", "literal": r"\ud800"}
