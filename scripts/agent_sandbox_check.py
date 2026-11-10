"""All three demo scenes end to end: agent → PayMart → guard (real Claude) → PayPal sandbox.

    .venv/bin/python scripts/agent_sandbox_check.py

Goes through POST /agent/runs, the same way the shopper screen does. Saved agent
runs are replayed, so only the first run of each scene pays for the agent.
Needs SANDBOX_VAULT_ID and ANTHROPIC_API_KEY in .env.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app import config, slips, store  # noqa: E402
from app.main import app  # noqa: E402

REQUEST = "Buy one pair of running shoes for flat feet, size US 9, under $150, delivered by Friday"
SCENES = [("careful", "ALLOW", "CAPTURED"), ("naive", "BLOCK", "VOIDED"), ("skim", "HOLD", "AUTHORIZED")]

for name in ("MARKETPLACE_SIGNING_KEY", "DEMO_AGENT_SIGNING_KEY", "CHECK_SIGNING_KEY"):
    if not getattr(config, name):
        setattr(config, name, json.dumps(slips.new_private_jwk()))
        print(f"(using a throwaway {name})")
if not (config.SANDBOX_VAULT_ID and config.ANTHROPIC_API_KEY):
    sys.exit("Set SANDBOX_VAULT_ID and ANTHROPIC_API_KEY in .env.")

user_key = slips.new_private_jwk()
ok = True
with TestClient(app) as client:
    client.post("/users/sandbox-shopper/keys", json={"name": "Sandbox Shopper", "public_jwk": slips.public_jwk(user_key)})
    agent_key = client.get("/agent/key").json()["public_jwk"]
    for mode, decision, payment in SCENES:
        # What the browser does: sign pieces 1 and 2 for the agent's key.
        body = {"user_id": "sandbox-shopper", "mode": mode,
                "shopping_slip": slips.sign(slips.shopping_slip(agent_key, REQUEST, [store.MARKETPLACE]), user_key),
                "money_slip": slips.sign(slips.money_slip(agent_key, 15000), user_key)}
        run_id = client.post("/agent/runs", json=body).json()["id"]
        run = client.get(f"/agent/runs/{run_id}").json()  # the background task has finished here
        order = run["order"] or {}
        good = (order.get("decision"), order.get("payment_status")) == (decision, payment)
        ok &= good
        print(f"{'✅' if good else '❌'} {mode:8} {run['status']:6} replayed={run['replayed']!s:5} ${run['cost_usd']:.4f}  "
              f"bought={[i['title'] for i in order.get('items', [])]}  {order.get('decision')}/{order.get('payment_status')}  "
              f"{order.get('reason_codes')} {run['error'] or ''}")
        if mode == "skim" and order.get("decision") == "HOLD":
            rejected = client.post(f"/orders/{order['id']}/reject").json()
            print(f"   user rejects → {rejected['payment_status']}")

print("\n✅ Agent + guard + PayPal sandbox work together" if ok else "\n❌ Something's off; see above")
