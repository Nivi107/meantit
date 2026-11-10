"""PayPal REST calls MeantIt needs: hold money, then capture, void or refund it.

One PayPalClient per sandbox app (PayMart, Fund). Amounts are integer cents,
like AP2; PayPal wants strings such as "12.50", so we convert at the edge.
"""

import time
import uuid

import httpx

from app import config


class PayPalError(Exception):
    """A PayPal call failed. `name` is PayPal's error code, e.g. ORDER_NOT_APPROVED."""

    def __init__(self, status: int, body: dict):
        self.status = status
        self.body = body
        self.name = body.get("name") or body.get("error") or "UNKNOWN"
        self.debug_id = body.get("debug_id")
        message = body.get("message") or body.get("error_description") or ""
        issues = [d.get("issue") for d in body.get("details", []) if d.get("issue")]
        if issues:
            message += f" ({', '.join(issues)})"
        super().__init__(f"PayPal {status} {self.name}: {message} [debug_id={self.debug_id}]")


def to_paypal_amount(cents: int) -> str:
    """1250 -> "12.50"."""
    if cents < 0:
        raise ValueError("amount must not be negative")
    return f"{cents // 100}.{cents % 100:02d}"


class PayPalClient:
    def __init__(self, client_id: str, secret: str, base_url: str, webhook_id: str = "",
                 http: httpx.Client | None = None):
        self.client_id = client_id
        self.secret = secret
        self.base_url = base_url.rstrip("/")
        self.webhook_id = webhook_id
        self.http = http or httpx.Client(timeout=30)
        self._token = None
        self._token_expires_at = 0.0

    # ---- plumbing ----

    def _get_token(self) -> str:
        # Tokens last about 9 hours; reuse one until a minute before it expires.
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token
        response = self.http.post(
            f"{self.base_url}/v1/oauth2/token",
            auth=(self.client_id, self.secret),
            data={"grant_type": "client_credentials"},
        )
        if response.status_code != 200:
            raise PayPalError(response.status_code, _json(response))
        body = response.json()
        self._token = body["access_token"]
        self._token_expires_at = time.time() + body.get("expires_in", 0)
        return self._token

    def _request(self, method: str, path: str, json: dict | None = None,
                 request_id: str | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self._get_token()}"}
        if method == "POST":
            # Same request_id on a retry means PayPal does the work only once.
            headers["PayPal-Request-Id"] = request_id or str(uuid.uuid4())
            headers["Prefer"] = "return=representation"
        response = self.http.request(method, f"{self.base_url}{path}", json=json, headers=headers)
        if response.status_code >= 400:
            raise PayPalError(response.status_code, _json(response))
        return _json(response)

    # ---- orders ----

    def create_order(self, amount_cents: int, *, currency: str = "USD", description: str = "",
                     reference_id: str = "", vault_id: str | None = None,
                     save_to_vault: bool = False, return_url: str = "", cancel_url: str = "",
                     request_id: str | None = None) -> dict:
        """Create an AUTHORIZE order.

        With vault_id (a saved PayPal account) there is no popup: PayPal approves and
        authorizes in one step, so the result is COMPLETED with the hold already made.
        With save_to_vault the shopper approves in a popup and the account is saved.
        """
        unit = {"amount": {"currency_code": currency, "value": to_paypal_amount(amount_cents)}}
        if description:
            unit["description"] = description[:127]
        if reference_id:
            unit["reference_id"] = reference_id
        body = {"intent": "AUTHORIZE", "purchase_units": [unit]}

        if vault_id:
            body["payment_source"] = {"paypal": {"vault_id": vault_id}}
        elif save_to_vault:
            body["payment_source"] = {"paypal": {
                "attributes": {"vault": {
                    "store_in_vault": "ON_SUCCESS",
                    "usage_type": "MERCHANT",
                    "customer_type": "CONSUMER",
                }},
                "experience_context": {
                    "return_url": return_url,
                    "cancel_url": cancel_url or return_url,
                    "user_action": "CONTINUE",
                    "shipping_preference": "NO_SHIPPING",
                },
            }}
        return self._request("POST", "/v2/checkout/orders", body, request_id)

    def get_order(self, order_id: str) -> dict:
        return self._request("GET", f"/v2/checkout/orders/{order_id}")

    def authorize_order(self, order_id: str, request_id: str | None = None) -> dict:
        """Place the hold after the shopper approved in the popup."""
        return self._request("POST", f"/v2/checkout/orders/{order_id}/authorize", {}, request_id)

    # ---- authorizations, captures, refunds ----

    def capture_authorization(self, authorization_id: str, request_id: str | None = None) -> dict:
        """Take the held money (guard said ALLOW). Captures the full amount and closes the hold."""
        return self._request("POST", f"/v2/payments/authorizations/{authorization_id}/capture",
                             {"final_capture": True}, request_id)

    def void_authorization(self, authorization_id: str, request_id: str | None = None) -> dict:
        """Release the held money (guard said BLOCK, or a HOLD timed out)."""
        return self._request("POST", f"/v2/payments/authorizations/{authorization_id}/void",
                             None, request_id)

    def get_authorization(self, authorization_id: str) -> dict:
        return self._request("GET", f"/v2/payments/authorizations/{authorization_id}")

    def refund_capture(self, capture_id: str, amount_cents: int | None = None, *,
                       currency: str = "USD", request_id: str | None = None) -> dict:
        """Give money back after capture. No amount means a full refund."""
        body = {}
        if amount_cents is not None:
            body["amount"] = {"currency_code": currency, "value": to_paypal_amount(amount_cents)}
        return self._request("POST", f"/v2/payments/captures/{capture_id}/refund", body, request_id)

    def get_capture(self, capture_id: str) -> dict:
        """Includes seller_receivable_breakdown.paypal_fee: what PayPal kept from the sale."""
        return self._request("GET", f"/v2/payments/captures/{capture_id}")

    # ---- payouts (the Protection Fund reimbursing PayMart) ----

    def create_payout(self, receiver_merchant_id: str, amount_cents: int, *, note: str, batch_id: str,
                      currency: str = "USD") -> dict:
        """Send money to one PayPal account. batch_id makes a retry pay only once."""
        body = {
            "sender_batch_header": {"sender_batch_id": batch_id, "email_subject": "MeantIt Protection Fund"},
            "items": [{
                "recipient_type": "PAYPAL_ID",
                "receiver": receiver_merchant_id,
                "amount": {"currency": currency, "value": to_paypal_amount(amount_cents)},
                "note": note,
                "sender_item_id": batch_id,
            }],
        }
        return self._request("POST", "/v1/payments/payouts", body, request_id=batch_id)

    def get_payout(self, payout_batch_id: str) -> dict:
        return self._request("GET", f"/v1/payments/payouts/{payout_batch_id}")

    # ---- webhooks ----

    def verify_webhook(self, headers, event: dict) -> bool:
        """Ask PayPal whether a webhook really came from PayPal and was meant for this app."""
        needed = {
            "auth_algo": "paypal-auth-algo",
            "cert_url": "paypal-cert-url",
            "transmission_id": "paypal-transmission-id",
            "transmission_sig": "paypal-transmission-sig",
            "transmission_time": "paypal-transmission-time",
        }
        lowered = {k.lower(): v for k, v in headers.items()}
        body = {field: lowered.get(header) for field, header in needed.items()}
        if not self.webhook_id or not all(body.values()):
            return False
        body["webhook_id"] = self.webhook_id
        body["webhook_event"] = event
        result = self._request("POST", "/v1/notifications/verify-webhook-signature", body)
        return result.get("verification_status") == "SUCCESS"


# ---- helpers for reading PayPal responses ----

def first_authorization(order: dict) -> dict | None:
    try:
        return order["purchase_units"][0]["payments"]["authorizations"][0]
    except (KeyError, IndexError):
        return None


def vault_id_of(order: dict) -> str | None:
    """The saved-account ID PayPal returns after a save_to_vault order is authorized."""
    vault = order.get("payment_source", {}).get("paypal", {}).get("attributes", {}).get("vault", {})
    return vault.get("id")


def _json(response: httpx.Response) -> dict:
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError:
        return {"raw": response.text[:500]}


# ---- one client per sandbox app, created on first use ----

_clients: dict[str, PayPalClient] = {}


def paymart() -> PayPalClient:
    """PayMart's app: takes shoppers' payments (Vault on)."""
    if "paymart" not in _clients:
        _clients["paymart"] = PayPalClient(config.PAYPAL_PAYMART_CLIENT_ID, config.PAYPAL_PAYMART_SECRET,
                                           config.PAYPAL_BASE_URL, config.PAYPAL_PAYMART_WEBHOOK_ID)
    return _clients["paymart"]


def fund() -> PayPalClient:
    """The Protection Fund's app: reimburses PayMart (Payouts on)."""
    if "fund" not in _clients:
        _clients["fund"] = PayPalClient(config.PAYPAL_FUND_CLIENT_ID, config.PAYPAL_FUND_SECRET,
                                        config.PAYPAL_BASE_URL, config.PAYPAL_FUND_WEBHOOK_ID)
    return _clients["fund"]
