from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb

from .db import get_conn
from .models import EventIn
from .observability import RequestIDMiddleware

app = FastAPI(title="RelayOps API")
app.add_middleware(RequestIDMiddleware)


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception):
    # Unhandled-error responses are sent outside the user middleware stack.
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal Server Error"},
        headers={"X-Request-ID": request.state.request_id},
    )


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc: RequestValidationError):
    # Keep validation errors JSON-safe even for non-standard NaN input.
    return JSONResponse(status_code=422, content={"detail": [
        {key: error[key] for key in ("loc", "msg", "type")}
        for error in exc.errors()
    ]})


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/metrics")
def metrics():
    with get_conn() as conn:
        counts = conn.execute("""
            SELECT count(*) FILTER (WHERE status='pending') AS pending,
                   count(*) FILTER (WHERE status='processing') AS processing,
                   count(*) FILTER (WHERE status='delivered') AS delivered,
                   count(*) FILTER (WHERE status='failed') AS failed,
                   (SELECT count(*) FROM delivery_attempts) AS delivery_attempts_total
            FROM events
        """).fetchone()
    lines = [
        "# HELP relayops_events Stored events by status.",
        "# TYPE relayops_events gauge",
        *[f'relayops_events{{status="{status}"}} {counts[status]}'
          for status in ("pending", "processing", "delivered", "failed")],
        "# HELP relayops_delivery_attempts_total Recorded delivery attempts.",
        "# TYPE relayops_delivery_attempts_total counter",
        f'relayops_delivery_attempts_total {counts["delivery_attempts_total"]}',
    ]
    return Response(
        content="\n".join(lines) + "\n",
        headers={"Content-Type": "text/plain; version=0.0.4; charset=utf-8"},
    )


@app.post("/events", status_code=202)
def create_event(event: EventIn, response: Response):
    with get_conn() as conn:
        row = conn.execute(
            """
            INSERT INTO events (event_id, type, payload)
            VALUES (%s, %s, %s)
            ON CONFLICT (event_id) DO NOTHING
            RETURNING event_id, status
            """,
            (event.event_id, event.type, Jsonb(event.payload)),
        ).fetchone()
        if row is None:
            # A separate statement sees the concurrent winner after its commit
            # under PostgreSQL's default READ COMMITTED isolation level.
            row = conn.execute(
                """
                SELECT event_id, status, (type = %s AND payload = %s) AS same_content
                FROM events WHERE event_id = %s
                """,
                (event.type, Jsonb(event.payload), event.event_id),
            ).fetchone()
            if not row.pop("same_content"):
                raise HTTPException(status_code=409, detail="event_id already exists with different content")
            response.status_code = 200
    return row


@app.get("/events/{event_id}")
def get_event(event_id: str):
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT event_id, type, payload, status, attempts, created_at, updated_at,
                   COALESCE((
                       SELECT jsonb_agg(jsonb_build_object(
                           'attempt', da.attempt,
                           'status_code', da.status_code,
                           'error', da.error,
                           'created_at', da.created_at
                       ) ORDER BY da.attempt)
                       FROM delivery_attempts da WHERE da.event_pk = events.id
                   ), '[]'::jsonb) AS delivery_attempts
            FROM events WHERE event_id = %s
            """,
            (event_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="event not found")

    return row
