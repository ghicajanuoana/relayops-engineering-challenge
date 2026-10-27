import json

import pytest

from worker import worker


def test_signature_matches_hmac_sha256_known_vector(monkeypatch):
    monkeypatch.setattr(worker, "WEBHOOK_SECRET", "\x0b" * 20)
    assert worker.sign(b"Hi There") == (
        "sha256=b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7"
    )


def test_serialization_contains_only_contract_fields():
    event = {"id": 1, "attempts": 0, "event_id": "evt-ț", "type": "test", "payload": {"nested": [None, True, "東京"]}}
    raw = worker.serialize_event(event)
    assert isinstance(raw, bytes)
    assert json.loads(raw) == {key: event[key] for key in ("event_id", "type", "payload")}
    assert "東京".encode("utf-8") in raw


@pytest.mark.parametrize("code,delivered", [(199, False), (200, True), (204, True), (299, True), (300, False), (401, False), (429, False), (503, False), (None, False)])
def test_only_2xx_is_delivered(code, delivered):
    assert worker.DeliveryResult(code).delivered is delivered


@pytest.mark.parametrize("code,retryable", [
    (None, True), (429, True), (500, True), (503, True), (599, True),
    (200, False), (299, False), (300, False), (400, False), (401, False), (404, False),
])
def test_temporary_failure_classification(code, retryable):
    assert worker.DeliveryResult(code).retryable is retryable


@pytest.mark.parametrize("value,seconds", [
    (None, 0), ("", 0), ("0", 0), ("12", 12), (" 3 ", 3),
    ("-1", 0), ("1.5", 0), ("invalid", 0), ("Wed, 21 Oct 2015 07:28:00 GMT", 0),
])
def test_retry_after_seconds(value, seconds):
    assert worker.parse_retry_after(value) == seconds


@pytest.mark.parametrize("attempt,header,delay", [
    (1, 0, 1), (2, 0, 2), (1, 5, 5), (2, 1, 2), (2, 10, 10),
])
def test_backoff_uses_larger_of_exponential_delay_and_retry_after(attempt, header, delay):
    assert worker.retry_delay(attempt, worker.DeliveryResult(503, retry_after_seconds=header)) == delay
