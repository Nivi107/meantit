import pytest
from fastapi.testclient import TestClient

from app import webhooks
from app.main import app

client = TestClient(app)

EVENT = {
    "id": "WH-1",
    "event_type": "PAYMENT.CAPTURE.COMPLETED",
    "create_time": "2026-11-02T00:00:00Z",
    "resource": {"id": "CAP-1", "status": "COMPLETED"},
}


class FakeClient:
    def __init__(self, verified):
        self.verified = verified
        self.calls = 0

    def verify_webhook(self, headers, event):
        self.calls += 1
        return self.verified


@pytest.fixture(autouse=True)
def clear_recent():
    webhooks.recent.clear()


def use_fake(monkeypatch, verified):
    fake = FakeClient(verified)
    monkeypatch.setitem(webhooks.CLIENTS, "paymart", lambda: fake)
    return fake


def test_verified_event_is_recorded(monkeypatch):
    use_fake(monkeypatch, verified=True)
    response = client.post("/webhooks/paypal/paymart", json=EVENT)
    assert response.json() == {"status": "received"}
    recent = client.get("/webhooks/paypal/recent").json()
    assert recent[0]["event_type"] == "PAYMENT.CAPTURE.COMPLETED"
    assert recent[0]["resource_id"] == "CAP-1"
    assert recent[0]["app"] == "paymart"


def test_unverified_event_is_rejected(monkeypatch):
    use_fake(monkeypatch, verified=False)
    response = client.post("/webhooks/paypal/paymart", json=EVENT)
    assert response.status_code == 400
    assert client.get("/webhooks/paypal/recent").json() == []


def test_duplicate_delivery_is_recorded_once(monkeypatch):
    use_fake(monkeypatch, verified=True)
    client.post("/webhooks/paypal/paymart", json=EVENT)
    second = client.post("/webhooks/paypal/paymart", json=EVENT)
    assert second.json() == {"status": "duplicate"}
    assert len(client.get("/webhooks/paypal/recent").json()) == 1


def test_unknown_app_is_404():
    assert client.post("/webhooks/paypal/other", json=EVENT).status_code == 404
