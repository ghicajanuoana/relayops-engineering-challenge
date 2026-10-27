import pytest
from pydantic import ValidationError

from app.models import EventIn


def test_valid_event():
    event = EventIn(event_id="evt-001", type="invoice.created", payload={"amount": 250})
    assert event.event_id == "evt-001"


@pytest.mark.parametrize(
    "data",
    [
        {"type": "invoice.created", "payload": {}},
        {"event_id": "", "type": "invoice.created", "payload": {}},
        {"event_id": "evt-001", "type": "invoice.created", "payload": "not-an-object"},
        {"event_id": "evt-001", "type": "", "payload": {}},
        {"event_id": "x" * 201, "type": "invoice.created", "payload": {}},
        {"event_id": "evt-001", "type": "x" * 201, "payload": {}},
        {"event_id": 123, "type": "invoice.created", "payload": {}},
        {"event_id": "evt-001", "type": "invoice.created", "payload": None},
        {"event_id": "evt-001", "type": "invoice.created", "payload": []},
        {"event_id": "evt-001", "type": "invoice.created"},
        {"event_id": "evt-001", "payload": {}},
        {"event_id": "evt\x00", "type": "invoice.created", "payload": {}},
        {"event_id": "evt-001", "type": "invoice.created", "payload": {"nested": ["\x00"]}},
        {"event_id": "evt-001", "type": "invoice.created", "payload": {"value": float("nan")}},
        {"event_id": "evt-001", "type": "invoice.created", "payload": {"value": float("inf")}},
    ],
)
def test_invalid_event(data):
    with pytest.raises(ValidationError):
        EventIn(**data)


def test_nested_payload_and_boundary_lengths():
    payload = {"nested": [None, True, 2, 1.5, {"unicode": "ț", "literal": r"\u0000"}]}
    event = EventIn(event_id="x" * 200, type="y" * 200, payload=payload)
    assert event.payload == payload
