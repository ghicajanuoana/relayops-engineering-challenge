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
