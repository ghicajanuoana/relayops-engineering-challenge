"""Concurrent webhook workers with transactional ownership and durable retries."""
import asyncio
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
import signal

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
MAX_ATTEMPTS = 3
ATTEMPT_TIMEOUT = 5


def sign(raw: bytes) -> str:
    return "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()


def serialize_event(event: dict) -> bytes:
    body = {key: event[key] for key in ("event_id", "type", "payload")}
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


@dataclass(frozen=True)
class DeliveryResult:
    status_code: int | None
    error: str | None = None
    retry_after_seconds: int = 0

    @property
    def delivered(self) -> bool:
        return self.status_code is not None and 200 <= self.status_code < 300

    @property
    def retryable(self) -> bool:
        return self.status_code is None or self.status_code == 429 or 500 <= self.status_code < 600


def parse_retry_after(value: str | None) -> int:
    """The receiver contract uses non-negative integer seconds, not HTTP dates."""
    if value is None:
        return 0
    value = value.strip()
    if not value.isascii() or not value.isdecimal():
        return 0
    try:
        return int(value)
    except ValueError:
        return 0


def retry_delay(attempt: int, result: DeliveryResult) -> int:
    return max(2 ** (attempt - 1), result.retry_after_seconds)


def fetch_pending(conn, limit: int | None = None):
    """Lock due rows until the caller's transaction ends; skip other owners."""
    return conn.execute(
        """
        SELECT id, event_id, type, payload, attempts
        FROM events
        WHERE status = 'pending' AND attempts < 3
          AND next_attempt_at <= clock_timestamp()
        ORDER BY next_attempt_at, id
        LIMIT %s
        FOR UPDATE SKIP LOCKED
        """,
        (BATCH_SIZE if limit is None else limit,),
    ).fetchall()


async def deliver(client: httpx.AsyncClient, event: dict) -> DeliveryResult:
    """Send once, signing exactly the UTF-8 bytes passed to HTTPX."""
    raw = serialize_event(event)
    headers = {"Content-Type": "application/json", "X-RelayOps-Signature": sign(raw)}
    try:
        response = await asyncio.wait_for(
            client.post(RECEIVER_URL, content=raw, headers=headers, follow_redirects=False),
            timeout=ATTEMPT_TIMEOUT,
        )
        return DeliveryResult(
            status_code=response.status_code,
            retry_after_seconds=parse_retry_after(response.headers.get("Retry-After")),
        )
    except TimeoutError:
        return DeliveryResult(status_code=None, error=f"TimeoutError: HTTP attempt exceeded {ATTEMPT_TIMEOUT} seconds")
    except httpx.RequestError as exc:
        return DeliveryResult(status_code=None, error=f"{type(exc).__name__}: {exc}")


def record_attempt(conn, event: dict, result: DeliveryResult) -> str:
    """Commit the attempt, outcome, and retry schedule together."""
    attempt = event["attempts"] + 1
    if result.delivered:
        status = "delivered"
    elif result.retryable and attempt < MAX_ATTEMPTS:
        status = "pending"
    else:
        status = "failed"
    delay = retry_delay(attempt, result) if status == "pending" else None
    with conn.transaction():
        conn.execute(
            """
            INSERT INTO delivery_attempts (event_pk, attempt, status_code, error)
            VALUES (%s, %s, %s, %s)
            """,
            (event["id"], attempt, result.status_code, result.error),
        )
        conn.execute(
            """
            UPDATE events SET status=%s, attempts=%s, updated_at=now(),
                next_attempt_at = CASE WHEN %s::double precision IS NULL THEN NULL
                    ELSE clock_timestamp() + %s::double precision * interval '1 second' END
            WHERE id=%s
            """,
            (status, attempt, delay, delay, event["id"]),
        )
    return status


async def process_pending(conn, client: httpx.AsyncClient, stop_event: asyncio.Event | None = None) -> int:
    processed = 0
    for _ in range(BATCH_SIZE):
        if stop_event is not None and stop_event.is_set():
            break
        # Own only the event being sent, not a batch waiting behind it.
        # A process/connection failure rolls this transaction back and releases
        # the row lock. Remote success before that rollback can cause a replay.
        with conn.transaction():
            events = fetch_pending(conn, limit=1)
            if not events:
                break
            event = events[0]
            result = await deliver(client, event)
            status = record_attempt(conn, event, result)
        processed += 1
        print(
            f"event_id={event['event_id']} attempt={event['attempts'] + 1} "
            f"status={status} "
            f"status_code={result.status_code} error={result.error}",
            flush=True,
        )
    return processed


async def run():
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_stop():
        if not stop_event.is_set():
            print("shutdown requested; finishing current attempt", flush=True)
            stop_event.set()

    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, request_stop)
    print(f"worker started, receiver={RECEIVER_URL}", flush=True)
    try:
        with psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=True) as conn:
            async with httpx.AsyncClient(timeout=ATTEMPT_TIMEOUT) as client:
                while not stop_event.is_set():
                    if not await process_pending(conn, client, stop_event):
                        try:
                            await asyncio.wait_for(stop_event.wait(), timeout=POLL_INTERVAL)
                        except TimeoutError:
                            pass
    finally:
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(signum)
    print("worker stopped", flush=True)


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
