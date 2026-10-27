import asyncio
import json
import logging
from pathlib import Path
import socket
import sys
import time

import httpx
import pytest

from app.observability import SafeFormatter, redact
from worker import worker


@pytest.mark.parametrize("secret_name", ["PGPASSWORD", "WEBHOOK_SECRET"])
def test_api_formatter_redacts_messages_and_tracebacks(monkeypatch, secret_name):
    secret = 'test-secret-with-"-quotes'
    monkeypatch.setenv(secret_name, secret)
    try:
        raise RuntimeError("sensitive error " + secret)
    except RuntimeError:
        record = logging.LogRecord("uvicorn.error", logging.ERROR, __file__, 1,
                                   "sensitive message %s", (secret,), sys.exc_info())
    formatted = SafeFormatter("%(message)s").format(record)
    assert secret not in formatted
    assert "[REDACTED]" in formatted
    assert secret not in redact(json.dumps({"path": secret}))


def test_api_formatter_redacts_password_from_legacy_database_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:private-password@db/test")
    assert "private-password" not in redact("connection failed: private-password")


def test_worker_logs_redact_secrets(monkeypatch, capsys):
    secret = 'test-secret-with-"-quotes'
    monkeypatch.setenv("WEBHOOK_SECRET", secret)
    worker.log_event("test", event_id=secret)
    output = capsys.readouterr().out
    assert secret not in output and json.dumps(secret)[1:-1] not in output
    assert "[REDACTED]" in output


def test_network_error_does_not_store_url_credentials(monkeypatch):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    monkeypatch.setattr(worker, "RECEIVER_URL", f"http://test:private-url-password@127.0.0.1:{port}/webhook?token=private-url-token")

    async def send():
        async with httpx.AsyncClient(timeout=1, trust_env=False) as client:
            return await worker.deliver(client, {"event_id": "secret-test", "type": "test", "payload": {}})

    result = asyncio.run(send())
    assert result.status_code is None and result.error.startswith("ConnectError:")
    assert "private-url-password" not in result.error and "private-url-token" not in result.error


@pytest.mark.parametrize("contents", [None, "invalid", "0", str(time.monotonic() + 1000)])
def test_worker_health_rejects_missing_invalid_or_stale_heartbeat(monkeypatch, tmp_path, contents):
    path = tmp_path / "heartbeat"
    if contents is not None:
        path.write_text(contents)
    monkeypatch.setattr(worker, "heartbeat_path", lambda pid: path)
    with pytest.raises(SystemExit, match="worker readiness check failed"):
        worker.check_health()


def test_worker_heartbeat_is_updated(monkeypatch, tmp_path):
    path = tmp_path / "heartbeat"
    monkeypatch.setattr(worker, "heartbeat_path", lambda pid: path)
    worker.heartbeat()
    assert 0 <= time.monotonic() - float(path.read_text()) < 1
