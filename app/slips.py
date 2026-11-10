"""The four signed pieces of an agent order (design doc, Module 1).

1. Shopping slip  (AP2 open Checkout Mandate)   signed by the user
2. Money slip     (AP2 open Payment Mandate)    signed by the user
3. Cart           (checkout_jwt)                signed by the marketplace
4. Final OK       (AP2 closed Checkout Mandate) signed by the agent

AP2 field names, but plain ES256 JWTs instead of full SD-JWT. Amounts are in
cents. Keys are JWKs (dicts) on P-256, the curve every browser's WebCrypto has.
"""

import base64
import hashlib
import json
import secrets
import time

import jwt
from cryptography.hazmat.primitives.asymmetric import ec

ALG = "ES256"

VCT_SHOPPING_SLIP = "mandate.checkout.open.1"
VCT_MONEY_SLIP = "mandate.payment.open.1"
VCT_FINAL_OK = "mandate.checkout.1"

INTENT_CONSTRAINT = "meantit.intent.1"


# ---- keys ----

def new_private_jwk() -> dict:
    """A fresh P-256 key pair as a JWK. Keep it secret: it includes the private part `d`."""
    key = ec.generate_private_key(ec.SECP256R1())
    return json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key))


def public_jwk(jwk: dict) -> dict:
    """Only the public part, safe to share or put in a slip's `cnf`."""
    if jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
        raise ValueError("key must be an EC P-256 JWK")
    return {"kty": "EC", "crv": "P-256", "x": jwk["x"], "y": jwk["y"]}


def load_jwk(text: str) -> dict | None:
    """Read a JWK from an environment variable (JSON). Empty means not set."""
    return json.loads(text) if text else None


def _key(jwk: dict, private: bool):
    data = jwk if private else public_jwk(jwk)
    return jwt.algorithms.ECAlgorithm.from_jwk(json.dumps(data))


# ---- signing and reading ----

def sign(claims: dict, private_jwk: dict) -> str:
    return jwt.encode(claims, _key(private_jwk, private=True), algorithm=ALG)


def read(token: str, public: dict) -> dict:
    """Check the signature and return the claims. Raises jwt.InvalidTokenError if it fails.

    Only ES256 is accepted, so a token claiming alg "none" or HS256 is rejected.
    Times are checked by the guard against its own clock, not here.
    """
    options = {"verify_exp": False, "verify_iat": False, "verify_nbf": False,
               "verify_aud": False, "verify_iss": False}
    return jwt.decode(token, _key(public, private=False), algorithms=[ALG], options=options)


def read_unverified(token: str) -> dict:
    """Claims without checking the signature. Only for finding out which key to check with."""
    return jwt.decode(token, options={"verify_signature": False})


def hash_of(token: str) -> str:
    """SHA-256 of a signed piece, base64url without padding (how AP2 links pieces)."""
    digest = hashlib.sha256(token.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _now(now: int | None) -> int:
    return int(time.time()) if now is None else now


# ---- the four pieces (claims only; sign them with sign()) ----

def shopping_slip(agent_key: dict, request: str, merchants: list[dict], *, valid_for: int = 3 * 86400,
                  locale: str = "en-US", now: int | None = None) -> dict:
    """Piece 1: "My agent may shop at these marketplaces, and this is what I want."

    agent_key is the agent's public JWK; only that agent can use this slip.
    merchants is a list of {"id", "name", "website"}.
    """
    iat = _now(now)
    return {
        "vct": VCT_SHOPPING_SLIP,
        "iat": iat,
        "exp": iat + valid_for,
        "cnf": {"jwk": public_jwk(agent_key)},
        "constraints": [
            {"type": "checkout.allowed_merchants", "allowed": merchants},
            {"type": INTENT_CONSTRAINT, "request": request, "locale": locale},
        ],
    }


def money_slip(agent_key: dict, max_cents: int, *, currency: str = "USD", min_cents: int = 0,
               valid_for: int = 3 * 86400, now: int | None = None) -> dict:
    """Piece 2: "Up to this amount, until this date." """
    iat = _now(now)
    return {
        "vct": VCT_MONEY_SLIP,
        "iat": iat,
        "exp": iat + valid_for,
        "cnf": {"jwk": public_jwk(agent_key)},
        "constraints": [
            {"type": "payment.amount_range", "currency": currency, "min": min_cents, "max": max_cents},
        ],
    }


def cart(marketplace_id: str, checkout_id: str, seller_id: str, items: list[dict], *,
         currency: str = "USD", delivery_estimate: str = "", valid_for: int = 15 * 60,
         now: int | None = None) -> dict:
    """Piece 3: "This exact cart, at this exact price." Signed by the marketplace.

    items is a list of {"sku", "title", "qty", "unit_price"} with unit_price in cents.
    """
    return {
        "iss": marketplace_id,
        "checkout_id": checkout_id,
        "seller_id": seller_id,
        "items": items,
        "total": sum(item["qty"] * item["unit_price"] for item in items),
        "currency": currency,
        "delivery_estimate": delivery_estimate,
        "exp": _now(now) + valid_for,
    }


def final_ok(shopping_slip_jwt: str, cart_jwt: str, marketplace_id: str, *,
             nonce: str | None = None, now: int | None = None) -> dict:
    """Piece 4: "I'm using the user's slips to buy this cart." Signed by the agent."""
    return {
        "vct": VCT_FINAL_OK,
        "checkout_jwt": cart_jwt,
        "checkout_hash": hash_of(cart_jwt),
        "sd_hash": hash_of(shopping_slip_jwt),
        "nonce": nonce or "n_" + secrets.token_urlsafe(16),
        "aud": marketplace_id,
        "iat": _now(now),
    }
