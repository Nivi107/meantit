"""Guard stage 1: a valid order passes; each kind of tampering is caught."""

import base64
import json

import pytest

from app import slips
from app.guard.verify import LIMIT_EXCEEDED, MANDATE_INVALID, NonceStore, verify

NOW = 1_800_000_000
USER = slips.new_private_jwk()
AGENT = slips.new_private_jwk()
PAYMART = slips.new_private_jwk()
OTHER_MARKET = slips.new_private_jwk()
STRANGER = slips.new_private_jwk()

MARKETPLACE_KEYS = {"mkt_paymart": slips.public_jwk(PAYMART), "mkt_other": slips.public_jwk(OTHER_MARKET)}
PAYMART_INFO = {"id": "mkt_paymart", "name": "PayMart", "website": "https://paymart.example"}
REQUEST = "Buy one pair of running shoes for flat feet, size US 9, under $150, delivered by Friday"
ITEMS = [{"sku": "RS-204-US9", "title": "StableRun 4 (US 9)", "qty": 1, "unit_price": 12900}]


def make_bundle(*, user=USER, agent=AGENT, money_agent=None, final_ok_signer=None,
                market_id="mkt_paymart", market_key=PAYMART, items=ITEMS, max_cents=15000,
                cart_currency="USD", request=REQUEST, slip_extra=None, cart_edit=None,
                slip_age=0, final_ok_age=0, cart_age=0):
    shopping = slips.shopping_slip(agent, request, [PAYMART_INFO], now=NOW - slip_age)
    if slip_extra:
        shopping["constraints"].append(slip_extra)
    money = slips.money_slip(money_agent or agent, max_cents, now=NOW - slip_age)
    cart_claims = slips.cart(market_id, "chk_91bd", "sel_trusted_sports", items,
                             currency=cart_currency, now=NOW - cart_age)
    if cart_edit:
        cart_edit(cart_claims)
    shopping_jwt = slips.sign(shopping, user)
    cart_jwt = slips.sign(cart_claims, market_key)
    ok = slips.final_ok(shopping_jwt, cart_jwt, market_id, now=NOW - final_ok_age)
    return {
        "shopping_slip": shopping_jwt,
        "money_slip": slips.sign(money, user),
        "cart": cart_jwt,
        "final_ok": slips.sign(ok, final_ok_signer or agent),
    }


def check(bundle, nonces=None):
    return verify(bundle, slips.public_jwk(USER), MARKETPLACE_KEYS, nonces or NonceStore(), now=NOW)


def assert_fails(result, check_name, code=MANDATE_INVALID):
    assert not result.ok
    assert (result.failure.check, result.failure.code) == (check_name, code), result.failure


# ---- the happy path ----

def test_valid_order_passes():
    result = check(make_bundle())
    assert result.ok, result.failure
    assert result.intent == REQUEST
    assert result.cart["total"] == 12900
    assert result.final_ok["aud"] == "mkt_paymart"


def test_hash_matches_ap2_style():
    # base64url SHA-256, no padding: 43 characters.
    assert len(slips.hash_of("abc")) == 43
    assert "=" not in slips.hash_of("abc")


# ---- signatures ----

def test_slip_signed_by_someone_else():
    assert_fails(check(make_bundle(user=STRANGER)), "user_signature")


def test_slip_edited_after_signing():
    bundle = make_bundle()
    header, payload, signature = bundle["money_slip"].split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    claims["constraints"][0]["max"] = 999999
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    bundle["money_slip"] = f"{header}.{forged}.{signature}"
    assert_fails(check(bundle), "user_signature")


def test_unsigned_token_is_rejected():
    bundle = make_bundle()
    none_header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode()
    payload = bundle["shopping_slip"].split(".")[1]
    bundle["shopping_slip"] = f"{none_header}.{payload}."
    assert_fails(check(bundle), "user_signature")


def test_final_ok_signed_by_a_different_agent():
    assert_fails(check(make_bundle(final_ok_signer=STRANGER)), "agent_signature")


def test_slips_name_different_agents():
    assert_fails(check(make_bundle(money_agent=STRANGER)), "agent_key")


def test_cart_not_signed_by_the_marketplace():
    assert_fails(check(make_bundle(market_key=STRANGER)), "marketplace_signature")


def test_cart_from_unknown_marketplace():
    assert_fails(check(make_bundle(market_id="mkt_unknown", market_key=STRANGER)), "marketplace_signature")


# ---- links between pieces ----

def test_cart_swapped_after_the_agent_said_ok():
    bundle = make_bundle()
    pricier = [{**ITEMS[0], "unit_price": 14900}]
    bundle["cart"] = slips.sign(slips.cart("mkt_paymart", "chk_91bd", "sel_trusted_sports", pricier, now=NOW),
                                PAYMART)
    assert_fails(check(bundle), "cart_link")


def test_final_ok_for_another_shopping_slip():
    bundle = make_bundle()
    other = make_bundle(request="Buy a 65-inch TV")
    bundle["shopping_slip"] = other["shopping_slip"]
    assert_fails(check(bundle), "shopping_slip_link")


# ---- time ----

def test_expired_slip():
    assert_fails(check(make_bundle(slip_age=4 * 86400)), "expired")


def test_expired_cart_offer():
    assert_fails(check(make_bundle(cart_age=16 * 60)), "expired")


def test_stale_final_ok():
    assert_fails(check(make_bundle(final_ok_age=16 * 60)), "expired")


def test_replayed_final_ok():
    nonces = NonceStore()
    bundle = make_bundle()
    assert check(bundle, nonces).ok
    assert_fails(check(bundle, nonces), "replay")


def test_failed_order_does_not_use_up_its_nonce():
    nonces = NonceStore()
    bundle = make_bundle()
    good_cart = bundle["cart"]
    bundle["cart"] = "x.y.z"
    assert not check(bundle, nonces).ok
    bundle["cart"] = good_cart
    assert check(bundle, nonces).ok


# ---- limits and content ----

def test_over_budget():
    assert_fails(check(make_bundle(max_cents=10000)), "amount_range", LIMIT_EXCEEDED)


def test_wrong_currency():
    assert_fails(check(make_bundle(cart_currency="EUR")), "amount_range", LIMIT_EXCEEDED)


def test_marketplace_the_user_did_not_allow():
    assert_fails(check(make_bundle(market_id="mkt_other", market_key=OTHER_MARKET)),
                 "allowed_merchants", LIMIT_EXCEEDED)


def test_cart_total_does_not_add_up():
    def lie(cart):
        cart["total"] = 100
    assert_fails(check(make_bundle(cart_edit=lie)), "cart_total")


def test_unknown_constraint_fails_closed():
    assert_fails(check(make_bundle(slip_extra={"type": "something.new"})), "unknown_constraint")


def test_empty_request():
    assert_fails(check(make_bundle(request="   ")), "intent")


@pytest.mark.parametrize("missing", ["shopping_slip", "money_slip", "cart", "final_ok"])
def test_missing_piece(missing):
    bundle = make_bundle()
    del bundle[missing]
    assert_fails(check(bundle), "pieces_present")
