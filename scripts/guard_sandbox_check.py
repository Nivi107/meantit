"""Run the guard against the real PayPal sandbox and database (no AI yet).

    .venv/bin/python scripts/guard_sandbox_check.py

A. Honest order, AI stand-in says every requirement is met → ALLOW → captured
B. No AI available → HOLD → the user rejects → voided
C. Over budget → BLOCK at stage 1 → PayPal never touched

Needs SANDBOX_VAULT_ID in .env. Uses throwaway signing keys if none are set.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app import config, keys, orders, slips, store  # noqa: E402
from app.guard.ai import NoAI  # noqa: E402
from app.guard.schemas import Checklist, InjectionVerdict, Limits, MatchResult, MatchVerdict, Requirement  # noqa: E402
from app.main import app  # noqa: E402

for name in ("MARKETPLACE_SIGNING_KEY", "DEMO_AGENT_SIGNING_KEY", "CHECK_SIGNING_KEY"):
    if not getattr(config, name):
        setattr(config, name, json.dumps(slips.new_private_jwk()))
        print(f"(using a throwaway {name})")
if not config.SANDBOX_VAULT_ID:
    sys.exit("Set SANDBOX_VAULT_ID in .env (from scripts/paypal_vault_auth_test.py).")


class AllMetAI:
    """Stand-in until Claude is wired in: everything matches, quoting the description."""
    prompt_version, models = "stand-in", {}

    def checklist(self, request, today):
        return Checklist(limits=Limits(max_qty=1, size="US 9"), missing=[],
                         requirements=[Requirement(id="R1", text="Is a running shoe", kind="category", must=True)])

    def match(self, requirements, items):
        return MatchResult(verdicts=[MatchVerdict(req=r.id, sku=i["sku"], verdict="met",
                                                  evidence_quote=i["description"][:40])
                                     for r in requirements for i in items])

    def classify(self, text):
        return InjectionVerdict(addresses_ai=False)


user_key = slips.new_private_jwk()
with TestClient(app) as client:
    client.post("/users/sandbox-shopper/keys", json={"name": "Sandbox Shopper", "public_jwk": slips.public_jwk(user_key)})

    def place(max_cents=15000):
        agent = keys.agent()
        cart_jwt = client.post("/store/checkouts", json={"items": [{"product_id": "p01", "size_us": 9}]}).json()["cart_jwt"]
        shopping = slips.sign(slips.shopping_slip(agent, "Running shoes for flat feet, size US 9, under $150",
                                                  [store.MARKETPLACE]), user_key)
        return client.post("/guard/checks", json={
            "user_id": "sandbox-shopper", "shopping_slip": shopping,
            "money_slip": slips.sign(slips.money_slip(agent, max_cents), user_key),
            "cart": cart_jwt, "final_ok": slips.sign(slips.final_ok(shopping, cart_jwt, "mkt_paymart"), agent),
        }).json()

    def show(label, order):
        paypal = order["paypal"]
        print(f"{label:34} decision={order['decision']:5} payment={order['payment_status']:10} "
              f"paypal_order={paypal['order_id']} reasons={order['reason_codes']}")

    orders.get_ai = AllMetAI
    a = place()
    show("A. honest order", a)

    orders.get_ai = NoAI
    b = place()
    show("B. no AI → hold", b)
    show("   user rejects", client.post(f"/orders/{b['id']}/reject").json())

    show("C. over budget", place(max_cents=10000))

ok = (a["payment_status"] == "CAPTURED" and b["decision"] == "HOLD")
print("\n✅ Guard + PayPal sandbox + database work together" if ok else "\n❌ Something's off; see above")
