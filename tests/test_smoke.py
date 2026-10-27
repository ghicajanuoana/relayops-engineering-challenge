"""End-to-end smoke tests. Require the stack: docker compose up --build"""
import time
import uuid


def test_create_and_get_event(api):
    event_id = f"evt-{uuid.uuid4()}"
    resp = api.post(
        "/events",
        json={"event_id": event_id, "type": "invoice.created", "payload": {"amount": 250}},
    )
    assert resp.status_code == 202

    resp = api.get(f"/events/{event_id}")
    assert resp.status_code == 200
    assert resp.json()["event_id"] == event_id


def test_event_is_delivered(api, receiver):
    event_id = f"evt-{uuid.uuid4()}"
    body = {"event_id": event_id, "type": "invoice.created", "payload": {}}
    assert api.post("/events", json=body).status_code == 202

    deadline = time.time() + 15
    while time.time() < deadline:
        deliveries = [r for r in receiver.get("/received").json() if r["event_id"] == event_id]
        response = api.get(f"/events/{event_id}")
        assert response.status_code == 200
        event = response.json()
        if deliveries and event["status"] == "delivered":
            break
        time.sleep(0.5)
    assert len(deliveries) == 1
    assert deliveries[0]["signature_valid"] is True
    assert deliveries[0]["status_code"] == 200
    assert deliveries[0]["body"] == body
    assert event["status"] == "delivered" and event["attempts"] == 1
    assert len(event["delivery_attempts"]) == 1
    assert event["delivery_attempts"][0]["status_code"] == 200
    assert event["delivery_attempts"][0]["error"] is None
