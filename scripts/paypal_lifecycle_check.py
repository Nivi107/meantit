"""Sandbox check of app/paypal.py: hold → capture → refund, and hold → void.

Uses the saved PayPal account (vault ID) from paypal_vault_auth_test.py, so
there is no popup. Put it in .env as SANDBOX_VAULT_ID, or pass it:

    .venv/bin/python scripts/paypal_lifecycle_check.py <vault_id>
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import paypal  # noqa: E402  (needs the path above)


def held_authorization(client, amount_cents, label):
    order = client.create_order(amount_cents, vault_id=vault_id, description=f"MeantIt check: {label}")
    if order["status"] == "APPROVED":
        order = client.authorize_order(order["id"])
    auth = paypal.first_authorization(order)
    if not auth:
        sys.exit(f"❌ {label}: no authorization on order {order['id']} (status {order['status']})")
    print(f"   ✅ order {order['id']} → authorization {auth['id']} {auth['status']} (money held)")
    return auth["id"]


vault_id = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SANDBOX_VAULT_ID", "")
if not vault_id:
    sys.exit("No vault ID. Copy it from the Vault test output into .env as SANDBOX_VAULT_ID.")

client = paypal.paymart()
results = []

try:
    print("A. Hold $10 → capture → refund $3 → refund the rest")
    auth_id = held_authorization(client, 1000, "capture path")
    capture = client.capture_authorization(auth_id)
    print(f"   ✅ capture {capture['id']} {capture['status']}")
    partial = client.refund_capture(capture["id"], 300)
    print(f"   ✅ refund {partial['id']} {partial['status']} ($3.00)")
    rest = client.refund_capture(capture["id"], 700)
    print(f"   ✅ refund {rest['id']} {rest['status']} ($7.00)")
    results.append(("Hold, capture, refund", "✅"))

    print("\nB. Hold $5 → void")
    auth_id = held_authorization(client, 500, "void path")
    client.void_authorization(auth_id)
    status = client.get_authorization(auth_id)["status"]
    print(f"   {'✅' if status == 'VOIDED' else '❌'} authorization {auth_id} {status}")
    results.append(("Hold, void", "✅" if status == "VOIDED" else "❌"))
except paypal.PayPalError as err:
    print(f"\n❌ {err}")
    for detail in err.body.get("details", []):
        print(f"   detail: {detail.get('issue')} — {detail.get('description')}")
    results.append(("Stopped at an error", "❌"))

print("\n──────── Summary ────────")
for step, mark in results:
    print(f"{step:24} {mark}")
