"""Request tracing for normal responses and application failures."""
import json
import logging
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders


logger = logging.getLogger("uvicorn.error")


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
            logger.log(logging.ERROR if status >= 500 else logging.INFO, json.dumps({
                "request_id": request_id,
                "method": scope["method"],
                "path": scope["path"],
                "status": status,
            }))
