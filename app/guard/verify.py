"""Guard stage 1: are the four signed pieces real, linked, current and within limits?

Pure code, no AI. Any failure means BLOCK. Checks run in order and stop at the
first failure, because nothing in a piece can be trusted once its signature fails.
"""

import time
from dataclasses import dataclass, field

import jwt

from app import slips

MANDATE_INVALID = "MANDATE_INVALID"
LIMIT_EXCEEDED = "LIMIT_EXCEEDED"

CLOCK_SKEW = 60            # seconds a piece's iat may be ahead of our clock
FINAL_OK_MAX_AGE = 15 * 60  # the agent's final OK must be this fresh
MAX_REQUEST_CHARS = 2000

KNOWN_CONSTRAINTS = {
    slips.VCT_SHOPPING_SLIP: {"checkout.allowed_merchants", slips.INTENT_CONSTRAINT},
    slips.VCT_MONEY_SLIP: {"payment.amount_range"},
}


@dataclass
class Failure:
    check: str    # which check failed, e.g. "agent_signature"
    code: str     # reason code for the decision table
    message: str  # plain-English explanation


@dataclass
class VerifyResult:
    failure: Failure | None = None
    # Claims of each piece, filled in as they are verified (for later stages and the evidence view).
    shopping_slip: dict = field(default_factory=dict)
    money_slip: dict = field(default_factory=dict)
    cart: dict = field(default_factory=dict)
    final_ok: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.failure is None

    @property
    def intent(self) -> str:
        """The user's own words from the shopping slip."""
        return _constraint(self.shopping_slip, slips.INTENT_CONSTRAINT).get("request", "")


class NonceStore:
    """Remembers used nonces so the same final OK can't be submitted twice.

    In memory for now; moves to the database with the rest of the order data.
    """

    def __init__(self):
        self._seen: dict[str, int] = {}

    def use(self, nonce: str, now: int) -> bool:
        """True the first time a nonce is seen, False after that."""
        self._seen = {n: t for n, t in self._seen.items() if t > now - 2 * FINAL_OK_MAX_AGE}
        if nonce in self._seen:
            return False
        self._seen[nonce] = now
        return True


class _Fail(Exception):
    def __init__(self, check: str, code: str, message: str):
        self.failure = Failure(check, code, message)


def verify(bundle: dict, user_key: dict, marketplace_keys: dict[str, dict], nonces: NonceStore,
           now: int | None = None) -> VerifyResult:
    """Verify an order's four pieces.

    bundle: {"shopping_slip", "money_slip", "cart", "final_ok"}, each a signed JWT string.
    user_key: the user's registered public JWK.
    marketplace_keys: marketplace ID → its public JWK.
    """
    now = int(time.time()) if now is None else now
    result = VerifyResult()
    try:
        _verify(bundle, user_key, marketplace_keys, nonces, now, result)
    except _Fail as fail:
        result.failure = fail.failure
    return result


def _verify(bundle, user_key, marketplace_keys, nonces, now, result):
    tokens = {name: bundle.get(name) for name in ("shopping_slip", "money_slip", "cart", "final_ok")}
    missing = [name for name, token in tokens.items() if not isinstance(token, str) or not token]
    if missing:
        raise _Fail("pieces_present", MANDATE_INVALID, f"Missing signed piece(s): {', '.join(missing)}.")

    # 1. The user signed the shopping slip and the money slip.
    result.shopping_slip = _read(tokens["shopping_slip"], user_key, "user_signature",
                                 "The shopping slip is not signed by this user.")
    result.money_slip = _read(tokens["money_slip"], user_key, "user_signature",
                              "The money slip is not signed by this user.")
    _expect_vct(result.shopping_slip, slips.VCT_SHOPPING_SLIP, "shopping slip")
    _expect_vct(result.money_slip, slips.VCT_MONEY_SLIP, "money slip")

    # 2. The final OK is signed by the agent the user named in `cnf`.
    agent_key = _cnf_key(result.shopping_slip, "shopping slip")
    if _cnf_key(result.money_slip, "money slip") != agent_key:
        raise _Fail("agent_key", MANDATE_INVALID, "The two slips name different agents.")
    result.final_ok = _read(tokens["final_ok"], agent_key, "agent_signature",
                            "The final OK is not signed by the agent the user allowed.")
    _expect_vct(result.final_ok, slips.VCT_FINAL_OK, "final OK")

    # 3. The pieces belong together.
    if result.final_ok.get("sd_hash") != slips.hash_of(tokens["shopping_slip"]):
        raise _Fail("shopping_slip_link", MANDATE_INVALID,
                    "The final OK points to a different shopping slip.")
    if (result.final_ok.get("checkout_jwt") != tokens["cart"]
            or result.final_ok.get("checkout_hash") != slips.hash_of(tokens["cart"])):
        raise _Fail("cart_link", MANDATE_INVALID, "The final OK points to a different cart.")

    # 4. The marketplace signed the cart, and the agent is buying from that marketplace.
    marketplace_id = slips.read_unverified(tokens["cart"]).get("iss")
    marketplace_key = marketplace_keys.get(marketplace_id)
    if not marketplace_key:
        raise _Fail("marketplace_signature", MANDATE_INVALID, f"Unknown marketplace {marketplace_id!r}.")
    result.cart = _read(tokens["cart"], marketplace_key, "marketplace_signature",
                        "The cart is not signed by the marketplace.")
    if result.final_ok.get("aud") != marketplace_id:
        raise _Fail("audience", MANDATE_INVALID, "The final OK was made for a different marketplace.")
    _check_cart_sums(result.cart)

    # 5. Nothing has expired.
    for piece, name in ((result.shopping_slip, "shopping slip"), (result.money_slip, "money slip")):
        if not isinstance(piece.get("exp"), int) or piece["exp"] <= now:
            raise _Fail("expired", MANDATE_INVALID, f"The {name} has expired.")
        if not isinstance(piece.get("iat"), int) or piece["iat"] > now + CLOCK_SKEW:
            raise _Fail("expired", MANDATE_INVALID, f"The {name} is dated in the future.")
    if not isinstance(result.cart.get("exp"), int) or result.cart["exp"] <= now:
        raise _Fail("expired", MANDATE_INVALID, "The cart's price offer has expired.")
    iat = result.final_ok.get("iat")
    if not isinstance(iat, int) or iat > now + CLOCK_SKEW or iat < now - FINAL_OK_MAX_AGE:
        raise _Fail("expired", MANDATE_INVALID, "The final OK is too old or dated in the future.")

    # 6. AP2 constraints: allowed marketplace, total within the amount range, and our intent field.
    _check_known_constraints(result.shopping_slip)
    _check_known_constraints(result.money_slip)
    allowed = _constraint(result.shopping_slip, "checkout.allowed_merchants").get("allowed")
    if not isinstance(allowed, list):
        raise _Fail("allowed_merchants", MANDATE_INVALID, "The shopping slip has no allowed-marketplace list.")
    if marketplace_id not in {m.get("id") for m in allowed if isinstance(m, dict)}:
        raise _Fail("allowed_merchants", LIMIT_EXCEEDED,
                    f"The user didn't allow shopping at {marketplace_id!r}.")
    amount = _constraint(result.money_slip, "payment.amount_range")
    if not isinstance(amount.get("max"), int) or not isinstance(amount.get("min", 0), int):
        raise _Fail("amount_range", MANDATE_INVALID, "The money slip has no valid amount range.")
    if amount.get("currency") != result.cart.get("currency"):
        raise _Fail("amount_range", LIMIT_EXCEEDED, "The cart's currency isn't the one the user allowed.")
    if not amount.get("min", 0) <= result.cart["total"] <= amount.get("max", -1):
        raise _Fail("amount_range", LIMIT_EXCEEDED,
                    f"The cart total {result.cart['total']} is outside the allowed range "
                    f"{amount.get('min', 0)}–{amount.get('max')} (cents).")
    request = _constraint(result.shopping_slip, slips.INTENT_CONSTRAINT).get("request")
    if not isinstance(request, str) or not request.strip() or len(request) > MAX_REQUEST_CHARS:
        raise _Fail("intent", MANDATE_INVALID, "The shopping slip has no usable request from the user.")

    # 7. Not a replay. Checked last, so only a fully valid order uses up its nonce.
    nonce = result.final_ok.get("nonce")
    if not isinstance(nonce, str) or not nonce or not nonces.use(nonce, now):
        raise _Fail("replay", MANDATE_INVALID, "This final OK has already been used.")


# ---- helpers ----

def _read(token, key, check, message):
    try:
        return slips.read(token, key)
    except (jwt.InvalidTokenError, ValueError, KeyError):
        raise _Fail(check, MANDATE_INVALID, message) from None


def _expect_vct(claims, vct, name):
    if claims.get("vct") != vct:
        raise _Fail("slip_type", MANDATE_INVALID, f"The {name} has the wrong type ({claims.get('vct')!r}).")


def _cnf_key(claims, name):
    try:
        return slips.public_jwk(claims["cnf"]["jwk"])
    except (KeyError, TypeError, ValueError):
        raise _Fail("agent_key", MANDATE_INVALID, f"The {name} doesn't name a valid agent key.") from None


def _constraint(claims, constraint_type) -> dict:
    for item in claims.get("constraints", []):
        if isinstance(item, dict) and item.get("type") == constraint_type:
            return item
    return {}


def _check_known_constraints(claims):
    # Fail closed: a limit we don't understand might be one we'd be ignoring.
    known = KNOWN_CONSTRAINTS[claims["vct"]]
    for item in claims.get("constraints", []):
        kind = item.get("type") if isinstance(item, dict) else None
        if kind not in known:
            raise _Fail("unknown_constraint", MANDATE_INVALID, f"Unknown constraint type {kind!r}.")


def _check_cart_sums(cart):
    items = cart.get("items")
    if not isinstance(items, list) or not items:
        raise _Fail("cart_total", MANDATE_INVALID, "The cart has no items.")
    for item in items:
        qty, price = item.get("qty"), item.get("unit_price")
        if not isinstance(qty, int) or qty < 1 or not isinstance(price, int) or price < 0:
            raise _Fail("cart_total", MANDATE_INVALID, "A cart item has an invalid quantity or price.")
    if cart.get("total") != sum(item["qty"] * item["unit_price"] for item in items):
        raise _Fail("cart_total", MANDATE_INVALID, "The cart total doesn't match its items.")
