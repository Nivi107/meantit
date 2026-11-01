from fastapi.testclient import TestClient

from app import config
from app.main import app

client = TestClient(app)


def test_health_reports_ok():
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert set(body["configured"]) == {"paypal_paymart", "paypal_fund", "database", "anthropic"}


def test_health_never_leaks_secrets():
    text = client.get("/health").text
    secrets = [config.PAYPAL_PAYMART_SECRET, config.PAYPAL_FUND_SECRET, config.ANTHROPIC_API_KEY, config.DATABASE_URL]
    for value in filter(None, secrets):
        assert value not in text
