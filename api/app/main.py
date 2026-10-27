from fastapi import FastAPI, HTTPException, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb

from .db import get_conn
from .models import EventIn

app = FastAPI(title="RelayOps API")


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
