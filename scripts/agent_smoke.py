"""Run the demo agent once per mode with real Claude, then the guard's checks on its cart (no PayPal).

    .venv/bin/python scripts/agent_smoke.py            # all three modes, about $0.03
    .venv/bin/python scripts/agent_smoke.py naive      # just one (naive uses no Claude)
"""

import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import agent, config, keys, slips, store  # noqa: E402
from app.guard import ai, pipeline  # noqa: E402

REQUEST = "Buy one pair of running shoes for flat feet, size US 9, under $150, delivered by Friday"
EXPECTED = {"careful": "ALLOW", "naive": "BLOCK", "skim": "HOLD"}

if not config.ANTHROPIC_API_KEY:
    sys.exit("Set ANTHROPIC_API_KEY in .env.")
for name in ("MARKETPLACE_SIGNING_KEY", "DEMO_AGENT_SIGNING_KEY"):
    if not getattr(config, name):
        setattr(config, name, json.dumps(slips.new_private_jwk()))
logging.basicConfig(level=logging.WARNING)


def show(step):
    detail = step.get("input") or ""
    print(f"   {step['kind']:7} {step.get('text', '')[:110]}  {json.dumps(detail) if detail else ''}")


total = 0.0
for mode in sys.argv[1:] or list(EXPECTED):
    print(f"\n== {mode} ==")
    try:
        shop = agent.shop_naively if mode == agent.NAIVE else lambda *a: agent.shop_with_claude(mode, *a)
        lines, cost = shop(REQUEST, 15000, show)
    except agent.AgentFailed as err:
        print(f"   agent failed: {err}")
        continue
    total += cost
    if not lines:
        print("   bought nothing")
        continue
    cart = store.create_checkout(store.CheckoutRequest(items=lines))["cart"]
    outcome = pipeline.run_checks(SimpleNamespace(cart=cart, intent=REQUEST), ai.default_ai())
    print(f"   agent ${cost:.4f}  → cart {[l['title'] for l in cart['items']]}")
    print(f"   guard: {outcome.decision} {outcome.reason_codes}  (demo expects {EXPECTED[mode]})")
print(f"\nagent total ${total:.4f} (guard calls ~$0.005 each on top)")
