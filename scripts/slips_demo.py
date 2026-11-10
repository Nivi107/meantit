"""Create the four signed pieces for one order, verify them, then try to cheat.

    .venv/bin/python scripts/slips_demo.py

Uses throwaway keys, so it needs nothing from .env.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import slips  # noqa: E402  (needs the path above)
from app.guard.verify import NonceStore, verify  # noqa: E402

user, agent, paymart, attacker = (slips.new_private_jwk() for _ in range(4))
marketplace_keys = {"mkt_paymart": slips.public_jwk(paymart)}
paymart_info = {"id": "mkt_paymart", "name": "PayMart", "website": "https://paymart.example"}
request = "Buy one pair of running shoes for flat feet, size US 9, under $150, delivered by Friday"
items = [{"sku": "RS-204-US9", "title": "StableRun 4 (US 9)", "qty": 1, "unit_price": 12900}]


def build(cart_items=items, max_cents=15000, final_ok_key=agent, cart_key=paymart):
    shopping = slips.sign(slips.shopping_slip(agent, request, [paymart_info]), user)
    money = slips.sign(slips.money_slip(agent, max_cents), user)
    cart = slips.sign(slips.cart("mkt_paymart", "chk_91bd", "sel_trusted_sports", cart_items,
                                 delivery_estimate="2026-11-06"), cart_key)
    ok = slips.sign(slips.final_ok(shopping, cart, "mkt_paymart"), final_ok_key)
    return {"shopping_slip": shopping, "money_slip": money, "cart": cart, "final_ok": ok}


def run(label, bundle, nonces=None):
    result = verify(bundle, slips.public_jwk(user), marketplace_keys, nonces or NonceStore())
    outcome = "✅ passes" if result.ok else f"⛔ BLOCK  {result.failure.code}: {result.failure.message}"
    print(f"{label:38} {outcome}")
    return result


bundle = build()
print("The four pieces (signed JWTs, shortened):")
for name, token in bundle.items():
    print(f"  {name:14} {token[:60]}…  ({len(token)} chars)")

print("\nWhat the user signed in the shopping slip:")
print(json.dumps(slips.read(bundle["shopping_slip"], slips.public_jwk(user)), indent=2)[:900])

print("\n──────── Verify ────────")
nonces = NonceStore()
run("Honest order", bundle, nonces)
run("Same order sent again (replay)", bundle, nonces)

swapped = dict(build())
pricier = [{**items[0], "unit_price": 14900}]
swapped["cart"] = slips.sign(slips.cart("mkt_paymart", "chk_91bd", "sel_trusted_sports", pricier), paymart)
run("Cart swapped after the agent's OK", swapped)

run("Cart signed by a fake marketplace", build(cart_key=attacker))

run("Agent's OK signed by another agent", build(final_ok_key=attacker))
run("Cart costs more than the user allowed", build(max_cents=10000))
