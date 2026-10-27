"""Concurrent webhook workers with transactional ownership and durable retries."""
import asyncio
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import signal
import time

import httpx
import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.environ.get("DATABASE_URL", "")
RECEIVER_URL = os.environ.get("RECEIVER_URL", "http://receiver:9000/webhook")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "1"))
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "10"))
MAX_ATTEMPTS = 3
ATTEMPT_TIMEOUT = 5


def log_event(event: str, **fields):
    message = json.dumps({"event": event, "pid": os.getpid(), **fields})
    for name in ("PGPASSWORD", "POSTGRES_PASSWORD", "WEBHOOK_SECRET"):
        secret = os.environ.get(name)
        if secret:
            for variant in (secret, json.dumps(secret)[1:-1]):
                message = message.replace(variant, "[REDACTED]")
    print(message, flush=True)


def heartbeat_path(pid: int) -> Path:
    return Path(f"/tmp/relayops-worker-{pid}.heartbeat")


def heartbeat():
    path = heartbeat_path(os.getpid())
    temporary = path.with_suffix(".tmp")
    temporary.write_text(str(time.monotonic()), encoding="utf-8")
    temporary.replace(path)


def check_health():
    try:
        age = time.monotonic() - float(heartbeat_path(1).read_text(encoding="utf-8"))
        if not 0 <= age <= max(15, POLL_INTERVAL + ATTEMPT_TIMEOUT + 5):
            raise ValueError("stale heartbeat")
        with psycopg.connect(DATABASE_URL, connect_timeout=3, options="-c statement_timeout=2000") as conn:
            conn.execute("SELECT next_attempt_at FROM events LIMIT 0")
    except Exception:
        raise SystemExit("worker readiness check failed") from None


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
        # Exception text can contain URL credentials or tokens; keep only type.
        return DeliveryResult(status_code=None, error=f"{type(exc).__name__}: webhook request failed")


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
        heartbeat()
        log_event(
            "delivery_attempt", event_id=event["event_id"], attempt=event["attempts"] + 1,
            status=status, status_code=result.status_code, error=result.error,
        )
    return processed


async def run():
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_stop():
        if not stop_event.is_set():
            log_event("shutdown_requested", message="finishing current attempt")
            stop_event.set()

    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, request_stop)
    if not WEBHOOK_SECRET:
        raise ValueError("WEBHOOK_SECRET must be configured")
    log_event("worker_started")
    try:
        with psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=True, connect_timeout=5) as conn:
            heartbeat()
            async with httpx.AsyncClient(timeout=ATTEMPT_TIMEOUT) as client:
                while not stop_event.is_set():
                    heartbeat()
                    if not await process_pending(conn, client, stop_event):
                        try:
                            await asyncio.wait_for(stop_event.wait(), timeout=POLL_INTERVAL)
                        except TimeoutError:
                            pass
    finally:
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(signum)
        heartbeat_path(os.getpid()).unlink(missing_ok=True)
    log_event("worker_stopped")


def main():
    try:
        asyncio.run(run())
    except Exception as exc:
        log_event("worker_failed", error_type=type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
