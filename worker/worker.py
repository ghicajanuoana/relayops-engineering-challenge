"""Single-worker delivery: one signed HTTP attempt per pending event.

Failures are terminal for now; retry scheduling and work claiming are deferred.
"""
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
import time

import httpx
import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://relayops:relayops@db:5432/relayops"
)
RECEIVER_URL = os.environ.get("RECEIVER_URL", "http://receiver:9000/webhook")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "1"))
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "10"))


def sign(raw: bytes) -> str:
    return "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()


def serialize_event(event: dict) -> bytes:
    body = {key: event[key] for key in ("event_id", "type", "payload")}
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


@dataclass(frozen=True)
class DeliveryResult:
    status_code: int | None
    error: str | None = None

    @property
    def delivered(self) -> bool:
        return self.status_code is not None and 200 <= self.status_code < 300


def fetch_pending(conn):
    return conn.execute(
        """
        SELECT id, event_id, type, payload, attempts
        FROM events
        WHERE status = 'pending'
        ORDER BY id
        LIMIT %s
        """,
        (BATCH_SIZE,),
    ).fetchall()


def deliver(client: httpx.Client, event: dict) -> DeliveryResult:
    """Send once, signing exactly the UTF-8 bytes passed to HTTPX."""
    raw = serialize_event(event)
    headers = {"Content-Type": "application/json", "X-RelayOps-Signature": sign(raw)}
    try:
        response = client.post(RECEIVER_URL, content=raw, headers=headers, follow_redirects=False)
        return DeliveryResult(status_code=response.status_code)
    except httpx.RequestError as exc:
        return DeliveryResult(status_code=None, error=f"{type(exc).__name__}: {exc}")


def record_attempt(conn, event: dict, result: DeliveryResult) -> None:
    """Commit the attempt and event state together, or roll both back."""
    attempt = event["attempts"] + 1
    with conn.transaction():
        conn.execute(
            """
            INSERT INTO delivery_attempts (event_pk, attempt, status_code, error)
            VALUES (%s, %s, %s, %s)
            """,
            (event["id"], attempt, result.status_code, result.error),
        )
        conn.execute(
            "UPDATE events SET status=%s, attempts=%s, updated_at=now() WHERE id=%s",
            ("delivered" if result.delivered else "failed", attempt, event["id"]),
        )


def process_pending(conn, client: httpx.Client) -> int:
    events = fetch_pending(conn)
    for event in events:
        result = deliver(client, event)
        record_attempt(conn, event, result)
        print(
            f"event_id={event['event_id']} attempt={event['attempts'] + 1} "
            f"status={'delivered' if result.delivered else 'failed'} "
            f"status_code={result.status_code} error={result.error}",
            flush=True,
        )
    return len(events)


def main():
    print(f"worker started, receiver={RECEIVER_URL}", flush=True)
    with psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=True) as conn, \
            httpx.Client(timeout=5) as client:
        while True:
            if not process_pending(conn, client):
                time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
