import os

import psycopg
import pytest

from test_api_integration import client


pytestmark = pytest.mark.skipif(
    not os.environ.get("RELAYOPS_TEST_DATABASE_URL"),
    reason="set RELAYOPS_TEST_DATABASE_URL to test real database metrics",
)


def samples(response):
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"
    assert response.text.endswith("\n")
    assert "# TYPE relayops_events gauge\n" in response.text
    assert "# TYPE relayops_delivery_attempts_total counter\n" in response.text
    return dict(line.split() for line in response.text.splitlines() if not line.startswith("#"))


def test_empty_metrics_include_every_status(client):
    assert samples(client.get("/metrics")) == {
        'relayops_events{status="pending"}': "0",
        'relayops_events{status="processing"}': "0",
        'relayops_events{status="delivered"}': "0",
        'relayops_events{status="failed"}': "0",
        "relayops_delivery_attempts_total": "0",
    }


def test_metrics_match_stored_events_and_history_without_mutation(client, database_url):
    with psycopg.connect(database_url) as conn:
        for index, status in enumerate(["pending", "pending", "processing", "delivered", "failed"]):
            pk = conn.execute(
                "INSERT INTO events(event_id,type,payload,status) VALUES (%s,'metrics.check','{}',%s) RETURNING id",
                (f"metrics-{index}", status),
            ).fetchone()[0]
            if status == "delivered":
                conn.execute("UPDATE events SET attempts=2 WHERE id=%s", (pk,))
                conn.execute("INSERT INTO delivery_attempts(event_pk,attempt,status_code) VALUES (%s,1,503),(%s,2,200)", (pk, pk))
            if status == "failed":
                conn.execute("UPDATE events SET attempts=1 WHERE id=%s", (pk,))
                conn.execute("INSERT INTO delivery_attempts(event_pk,attempt,error) VALUES (%s,1,'connection refused')", (pk,))
    with psycopg.connect(database_url) as conn:
        before = conn.execute("SELECT * FROM events ORDER BY id").fetchall()
        before_history = conn.execute("SELECT * FROM delivery_attempts ORDER BY id").fetchall()
    expected = {
        'relayops_events{status="pending"}': "2",
        'relayops_events{status="processing"}': "1",
        'relayops_events{status="delivered"}': "1",
        'relayops_events{status="failed"}': "1",
        "relayops_delivery_attempts_total": "3",
    }
    assert samples(client.get("/metrics")) == expected
    assert samples(client.get("/metrics")) == expected
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT * FROM events ORDER BY id").fetchall() == before
        assert conn.execute("SELECT * FROM delivery_attempts ORDER BY id").fetchall() == before_history
