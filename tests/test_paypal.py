"""PayPalClient tests against a fake PayPal (no network)."""

import json
import time

import httpx
import pytest

from app.paypal import PayPalClient, PayPalError, first_authorization, to_paypal_amount, vault_id_of


class FakePayPal:
    """Records every request and answers from a path → (status, body) table."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 32400})
        status, body = self.routes[(request.method, request.url.path)]
        return httpx.Response(status, json=body)

    def api_calls(self):
        return [r for r in self.requests if r.url.path != "/v1/oauth2/token"]


def make_client(routes, webhook_id="WH-1"):
    fake = FakePayPal(routes)
    client = PayPalClient("id", "secret", "https://paypal.test", webhook_id,
                          http=httpx.Client(transport=httpx.MockTransport(fake)))
    return client, fake


def body_of(request):
    return json.loads(request.content) if request.content else None


def test_to_paypal_amount():
    assert to_paypal_amount(1250) == "12.50"
    assert to_paypal_amount(5) == "0.05"
    assert to_paypal_amount(10000) == "100.00"
    with pytest.raises(ValueError):
        to_paypal_amount(-1)


def test_token_is_fetched_once_and_reused():
    client, fake = make_client({("GET", "/v2/checkout/orders/O1"): (200, {"id": "O1"})})
    client.get_order("O1")
    client.get_order("O1")
    token_calls = [r for r in fake.requests if r.url.path == "/v1/oauth2/token"]
    assert len(token_calls) == 1
    assert fake.api_calls()[0].headers["Authorization"] == "Bearer tok"


def test_create_order_with_saved_account_has_no_popup_fields():
    client, fake = make_client({("POST", "/v2/checkout/orders"): (201, {"id": "O1", "status": "COMPLETED"})})
    client.create_order(1250, vault_id="V1", reference_id="cart-1")
    sent = body_of(fake.api_calls()[0])
    assert sent["intent"] == "AUTHORIZE"
    assert sent["purchase_units"][0]["amount"] == {"currency_code": "USD", "value": "12.50"}
    assert sent["purchase_units"][0]["reference_id"] == "cart-1"
    assert sent["payment_source"] == {"paypal": {"vault_id": "V1"}}


def test_create_order_that_saves_the_account():
    client, fake = make_client({("POST", "/v2/checkout/orders"): (201, {"id": "O1"})})
    client.create_order(1000, save_to_vault=True, return_url="https://x.test/back")
    paypal = body_of(fake.api_calls()[0])["payment_source"]["paypal"]
    assert paypal["attributes"]["vault"]["store_in_vault"] == "ON_SUCCESS"
    assert paypal["experience_context"]["return_url"] == "https://x.test/back"


def test_posts_send_request_id_and_keep_the_one_given():
    client, fake = make_client({("POST", "/v2/payments/authorizations/A1/capture"): (201, {"id": "C1"})})
    client.capture_authorization("A1", request_id="capture-cart-1")
    client.capture_authorization("A1")
    first, second = fake.api_calls()
    assert first.headers["PayPal-Request-Id"] == "capture-cart-1"
    assert second.headers["PayPal-Request-Id"] not in ("", "capture-cart-1")
    assert body_of(first) == {"final_capture": True}


def test_refund_full_and_partial():
    client, fake = make_client({("POST", "/v2/payments/captures/C1/refund"): (201, {"id": "R1"})})
    client.refund_capture("C1")
    client.refund_capture("C1", 300)
    full, partial = fake.api_calls()
    assert body_of(full) == {}
    assert body_of(partial) == {"amount": {"currency_code": "USD", "value": "3.00"}}


def test_errors_carry_paypal_name_and_debug_id():
    client, _ = make_client({("POST", "/v2/checkout/orders/O1/authorize"): (422, {
        "name": "UNPROCESSABLE_ENTITY", "debug_id": "dbg1",
        "details": [{"issue": "ORDER_NOT_APPROVED"}],
    })})
    with pytest.raises(PayPalError) as caught:
        client.authorize_order("O1")
    assert caught.value.status == 422
    assert caught.value.name == "UNPROCESSABLE_ENTITY"
    assert caught.value.debug_id == "dbg1"
    assert "ORDER_NOT_APPROVED" in str(caught.value)


WEBHOOK_HEADERS = {
    "PAYPAL-AUTH-ALGO": "SHA256withRSA",
    "PAYPAL-CERT-URL": "https://api.sandbox.paypal.com/cert",
    "PAYPAL-TRANSMISSION-ID": "t1",
    "PAYPAL-TRANSMISSION-SIG": "sig",
    "PAYPAL-TRANSMISSION-TIME": "2026-11-02T00:00:00Z",
}


def test_verify_webhook_asks_paypal_with_our_webhook_id():
    client, fake = make_client({("POST", "/v1/notifications/verify-webhook-signature"):
                                (200, {"verification_status": "SUCCESS"})})
    event = {"id": "WH-EVT-1", "event_type": "PAYMENT.CAPTURE.COMPLETED"}
    assert client.verify_webhook(WEBHOOK_HEADERS, event) is True
    sent = body_of(fake.api_calls()[0])
    assert sent["webhook_id"] == "WH-1"
    assert sent["transmission_id"] == "t1"
    assert sent["webhook_event"] == event


def test_verify_webhook_rejects_failure_and_missing_headers():
    client, fake = make_client({("POST", "/v1/notifications/verify-webhook-signature"):
                                (200, {"verification_status": "FAILURE"})})
    assert client.verify_webhook(WEBHOOK_HEADERS, {}) is False
    assert client.verify_webhook({}, {}) is False
    assert len(fake.api_calls()) == 1  # missing headers never reach PayPal


def test_response_helpers():
    order = {
        "payment_source": {"paypal": {"attributes": {"vault": {"id": "V1"}}}},
        "purchase_units": [{"payments": {"authorizations": [{"id": "A1"}]}}],
    }
    assert vault_id_of(order) == "V1"
    assert first_authorization(order)["id"] == "A1"
    assert vault_id_of({}) is None
    assert first_authorization({}) is None


def test_a_network_failure_is_a_paypal_error(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    def no_dns(request):
        raise httpx.ConnectError("[Errno -3] Temporary failure in name resolution")
    client = PayPalClient("id", "secret", "https://paypal.test", http=httpx.Client(transport=httpx.MockTransport(no_dns)))
    with pytest.raises(PayPalError) as err:
        client.get_order("O1")
    assert err.value.name == "NETWORK_ERROR"  # so a checkout ends FAILED instead of stuck at CREATED


def test_a_moment_without_network_is_retried(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    calls = []
    def blip(request):
        calls.append(request.url.path)
        if len(calls) == 1:
            raise httpx.ConnectError("[Errno -3] Temporary failure in name resolution")
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(200, json={"id": "O1", "status": "APPROVED"})
    client = PayPalClient("id", "secret", "https://paypal.test", http=httpx.Client(transport=httpx.MockTransport(blip)))
    assert client.get_order("O1")["status"] == "APPROVED"
    assert len(calls) == 3  # the token request twice, then the order
