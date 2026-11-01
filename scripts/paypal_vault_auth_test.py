#!/usr/bin/env python3
"""Check that a saved PayPal account (Vault) works with intent AUTHORIZE.

Steps:
  1. Get an access token (PayMart app).
  2. Create order #1 ($10, AUTHORIZE) that also saves the shopper's PayPal.
  3. You approve it in the browser as the sandbox Demo Shopper.
  4. Authorize order #1 and read the vault ID.
  5. Create order #2 ($12, AUTHORIZE) using only the vault ID (no popup).
  6. Void both authorizations and confirm they are VOIDED.

Usage (from the project folder):
  python3 scripts/paypal_vault_auth_test.py

Needs only the Python standard library. Reads credentials from .env.
"""

import json
import sys
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from base64 import b64encode
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


class PayPalError(Exception):
    def __init__(self, status, body):
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}")


def load_env(path):
    if not path.exists():
        sys.exit(f"Could not find {path}. Create it with your sandbox credentials first.")
    env = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def request(method, url, token=None, body=None, basic_auth=None, form=None):
    headers = {}
    data = None
    if basic_auth:
        headers["Authorization"] = "Basic " + b64encode(basic_auth.encode()).decode()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if form is not None:
        data = form.encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if method == "POST":
        # Unique per request, so a retried POST never charges twice.
        headers["PayPal-Request-Id"] = str(uuid.uuid4())

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as err:
        raw = err.read()
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = {"raw": raw.decode(errors="replace")}
        raise PayPalError(err.code, parsed) from None


def show_error(step, err):
    body = err.body
    print(f"\n❌ {step} failed (HTTP {err.status})")
    for key in ("name", "message", "error", "error_description", "debug_id"):
        if key in body:
            print(f"   {key}: {body[key]}")
    for detail in body.get("details", []):
        print(f"   detail: {detail.get('issue')} — {detail.get('description')}")
    if "raw" in body:
        print(f"   {body['raw'][:500]}")


def first_authorization(order):
    try:
        return order["purchase_units"][0]["payments"]["authorizations"][0]
    except (KeyError, IndexError):
        return None


def main():
    env = load_env(ENV_PATH)
    api = env.get("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com").rstrip("/")
    client_id = env.get("PAYPAL_PAYMART_CLIENT_ID")
    secret = env.get("PAYPAL_PAYMART_SECRET")
    if not client_id or not secret:
        sys.exit("PAYPAL_PAYMART_CLIENT_ID or PAYPAL_PAYMART_SECRET is empty in .env")
    if "sandbox" not in api:
        sys.exit(f"PAYPAL_BASE_URL is {api}. This test must run against the sandbox.")

    results = {}
    auth_ids = []

    # Step 1: token
    print("Step 1: Getting an access token...")
    try:
        _, data = request("POST", f"{api}/v1/oauth2/token",
                          basic_auth=f"{client_id}:{secret}",
                          form="grant_type=client_credentials")
    except PayPalError as err:
        show_error("Getting a token", err)
        print("   → Check the PayMart client ID and secret in .env.")
        return 1
    token = data["access_token"]
    results["1. Token"] = "✅"
    print("   ✅ Got a token")

    # Step 2: order #1 that saves PayPal
    print("\nStep 2: Creating order #1 ($10, AUTHORIZE, save PayPal)...")
    order1_body = {
        "intent": "AUTHORIZE",
        "purchase_units": [{"amount": {"currency_code": "USD", "value": "10.00"}}],
        "payment_source": {"paypal": {
            "attributes": {"vault": {
                "store_in_vault": "ON_SUCCESS",
                "usage_type": "MERCHANT",
                "customer_type": "CONSUMER",
            }},
            "experience_context": {
                "return_url": "https://example.com/return",
                "cancel_url": "https://example.com/cancel",
                "shipping_preference": "NO_SHIPPING",
                "user_action": "CONTINUE",
            },
        }},
    }
    try:
        _, order1 = request("POST", f"{api}/v2/checkout/orders", token, order1_body)
    except PayPalError as err:
        show_error("Creating order #1", err)
        print("   → If it mentions vault or permissions, turn on Vault in the app's Features.")
        return 1
    order1_id = order1["id"]
    approve_url = next((l["href"] for l in order1.get("links", [])
                        if l.get("rel") in ("payer-action", "approve")), None)
    print(f"   ✅ Order {order1_id}, status {order1['status']}")

    # Step 3: shopper approves
    shopper = env.get("SANDBOX_SHOPPER_EMAIL", "your Demo Shopper sandbox email")
    print("\nStep 3: Approve the payment in your browser.")
    print(f"   Link: {approve_url}")
    print(f"   Log in as {shopper} (sandbox account, not your real PayPal).")
    print("   Click Continue/Agree. You'll land on an example.com page — that's expected.")
    try:
        webbrowser.open(approve_url)
    except Exception:
        pass

    # Step 4: authorize order #1, read the vault ID
    order1_auth = None
    for attempt in range(3):
        input("\n   Press Enter once you've approved it... ")
        print("\nStep 4: Authorizing order #1...")
        try:
            _, order1_auth = request("POST", f"{api}/v2/checkout/orders/{order1_id}/authorize",
                                     token, {})
            break
        except PayPalError as err:
            if (err.body.get("details") or [{}])[0].get("issue") == "ORDER_NOT_APPROVED" and attempt < 2:
                print("   ⚠️  Not approved yet. Open the link, approve, then press Enter again.")
                continue
            show_error("Authorizing order #1", err)
            return 1

    auth1 = first_authorization(order1_auth)
    if auth1:
        auth_ids.append(auth1["id"])
        print(f"   ✅ Authorization {auth1['id']}, status {auth1['status']} (money held)")

    vault = order1_auth.get("payment_source", {}).get("paypal", {}).get("attributes", {}).get("vault", {})
    vault_id = vault.get("id")
    print(f"   Vault status: {vault.get('status')}")

    if not vault_id:
        customer_id = vault.get("customer", {}).get("id")
        if customer_id:
            print("   Vault ID not returned yet; checking saved payment tokens...")
            for _ in range(6):
                time.sleep(5)
                try:
                    _, tokens = request(
                        "GET", f"{api}/v3/vault/payment-tokens?customer_id={customer_id}", token)
                except PayPalError as err:
                    show_error("Looking up the vault token", err)
                    break
                if tokens.get("payment_tokens"):
                    vault_id = tokens["payment_tokens"][0]["id"]
                    break

    if not vault_id:
        results["4. Vault ID received"] = "❌"
        print("   ❌ No vault ID. Check that Vault is on for the PayMart app.")
        void_all(api, token, auth_ids, results)
        print_summary(results)
        return 1
    results["4. Vault ID received"] = "✅"
    print(f"   ✅ Vault ID: {vault_id}")

    # Step 5: order #2 with only the vault ID
    print("\nStep 5: Creating order #2 ($12, AUTHORIZE) with only the vault ID (no popup)...")
    order2_body = {
        "intent": "AUTHORIZE",
        "purchase_units": [{"amount": {"currency_code": "USD", "value": "12.00"}}],
        "payment_source": {"paypal": {"vault_id": vault_id}},
    }
    try:
        _, order2 = request("POST", f"{api}/v2/checkout/orders", token, order2_body)
    except PayPalError as err:
        show_error("Creating order #2 with the vault ID", err)
        results["5. Vault order held money"] = "❌ (create failed)"
        void_all(api, token, auth_ids, results)
        print_summary(results)
        return 1

    print(f"   Order {order2['id']}, status {order2['status']}")
    auth2 = first_authorization(order2)
    if not auth2 and order2["status"] == "APPROVED":
        print("   Order is APPROVED; authorizing it...")
        try:
            _, order2 = request("POST", f"{api}/v2/checkout/orders/{order2['id']}/authorize",
                                token, {})
            auth2 = first_authorization(order2)
        except PayPalError as err:
            show_error("Authorizing order #2", err)

    if auth2 and auth2.get("status") in ("CREATED", "PENDING"):
        auth_ids.append(auth2["id"])
        results["5. Vault order held money"] = f"✅ (order {order2['status']}, auth {auth2['status']})"
        print(f"   ✅ Authorization {auth2['id']}, status {auth2['status']} — held with no popup!")
    else:
        if auth2:
            auth_ids.append(auth2["id"])
        results["5. Vault order held money"] = f"❌ (order status {order2['status']})"
        print("   ❌ No held authorization. Full response:")
        print(json.dumps(order2, indent=2)[:3000])

    # Step 6: void everything
    void_all(api, token, auth_ids, results)
    print_summary(results)
    return 0 if all(v.startswith("✅") for v in results.values()) else 1


def void_all(api, token, auth_ids, results):
    print("\nStep 6: Voiding the holds...")
    ok = True
    for auth_id in auth_ids:
        try:
            request("POST", f"{api}/v2/payments/authorizations/{auth_id}/void", token)
        except PayPalError as err:
            show_error(f"Voiding {auth_id}", err)
        try:
            _, auth = request("GET", f"{api}/v2/payments/authorizations/{auth_id}", token)
            status = auth.get("status")
        except PayPalError:
            status = "UNKNOWN"
        mark = "✅" if status == "VOIDED" else "❌"
        ok = ok and status == "VOIDED"
        print(f"   {mark} {auth_id}: {status}")
    if auth_ids:
        results["6. All holds voided"] = "✅" if ok else "❌"


def print_summary(results):
    print("\n──────── Summary ────────")
    for step, outcome in results.items():
        print(f"{step:32} {outcome}")
    if results.get("5. Vault order held money", "").startswith("✅"):
        print("\n🎉 Vault + AUTHORIZE works: the agent can pay with a saved account and the")
        print("   money is held until the guard decides.")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit("\nStopped. Any held authorizations expire on their own after 29 days.")
