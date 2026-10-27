"""Real Nginx response headers, including gateway-generated rejection responses."""
import os
import re
from uuid import uuid4

import pytest

from test_api_http import live_client, live_event


pytestmark = pytest.mark.skipif(
    not os.environ.get("RELAYOPS_BASE_URL"),
    reason="set RELAYOPS_BASE_URL to test the real gateway",
)


@pytest.mark.parametrize("provided", [False, True])
@pytest.mark.parametrize("scenario,status", [
    ("health", 200), ("metrics", 200), ("missing", 404),
    ("validation", 422), ("method", 405), ("oversized", 413),
])
def test_gateway_ids_on_success_and_errors(live_client, provided, scenario, status):
    request_id = "gateway-test-" + uuid4().hex if provided else None
    headers = {"X-Request-ID": request_id} if provided else {}
    if scenario == "validation":
        response = live_client.post("/events", json={}, headers=headers)
    elif scenario == "method":
        response = live_client.post("/health", headers=headers)
    elif scenario == "oversized":
        response = live_client.post("/events", content=b"x" * (3 * 1024 * 1024), headers=headers)
    else:
        response = live_client.get({"health": "/health", "metrics": "/metrics", "missing": "/missing"}[scenario], headers=headers)
    assert response.status_code == status
    returned_id = response.headers["X-Request-ID"]
    assert response.headers.get_list("X-Request-ID") == [returned_id]
    if provided:
        assert returned_id == request_id
    else:
        assert re.fullmatch(r"[0-9a-f]{32}", returned_id)


@pytest.mark.parametrize("provided", [False, True])
def test_event_response_ids(live_client, live_event, provided):
    request_id = "event-trace-" + uuid4().hex if provided else None
    headers = {"X-Request-ID": request_id} if provided else {}
    requests = [
        (live_event, 202), (live_event, 200), ({**live_event, "type": "other"}, 409),
    ]
    ids = []
    for body, status in requests:
        response = live_client.post("/events", json=body, headers=headers)
        assert response.status_code == status
        returned_id = response.headers["X-Request-ID"]
        assert response.headers.get_list("X-Request-ID") == [returned_id]
        if provided:
            assert returned_id == request_id
        else:
            assert re.fullmatch(r"[0-9a-f]{32}", returned_id)
        ids.append(returned_id)
    assert len(set(ids)) == (1 if provided else 3)
