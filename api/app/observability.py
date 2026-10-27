"""Request tracing for normal responses and application failures."""
import json
import logging
import os
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders


logger = logging.getLogger("uvicorn.error")


def redact(message: str) -> str:
    secrets = [os.environ.get(name) for name in ("PGPASSWORD", "POSTGRES_PASSWORD", "WEBHOOK_SECRET", "DATABASE_URL")]
    if os.environ.get("DATABASE_URL"):
        from psycopg.conninfo import conninfo_to_dict
        try:
            secrets.append(conninfo_to_dict(os.environ["DATABASE_URL"]).get("password"))
        except Exception:
            pass
    for secret in secrets:
        if secret:
            from urllib.parse import quote, quote_plus
            for variant in (secret, json.dumps(secret)[1:-1], quote(secret, safe=""), quote_plus(secret)):
                message = message.replace(variant, "[REDACTED]")
    return message


class SafeFormatter(logging.Formatter):
    def format(self, record):
        return redact(super().format(record))


class RequestIDMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = Headers(scope=scope).get("x-request-id") or uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        status = 500

        async def send_with_id(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            logger.log(logging.ERROR if status >= 500 else logging.INFO, redact(json.dumps({
                "request_id": request_id,
                "method": scope["method"],
                "path": scope["path"],
                "status": status,
            })))
