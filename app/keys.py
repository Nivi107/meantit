"""The signing keys our backend holds in the demo (it plays PayMart, the demo agent and MeantIt)."""

from fastapi import HTTPException

from app import config, slips


def _load(name: str) -> dict:
    jwk = slips.load_jwk(getattr(config, name))
    if not jwk:
        raise HTTPException(503, f"{name} is not set (run scripts/generate_keys.py)")
    return jwk


def marketplace() -> dict:
    """PayMart's private key: signs carts."""
    return _load("MARKETPLACE_SIGNING_KEY")


def agent() -> dict:
    """The demo agent's private key: signs the final OK."""
    return _load("DEMO_AGENT_SIGNING_KEY")


def check() -> dict:
    """MeantIt's private key: signs Check records."""
    return _load("CHECK_SIGNING_KEY")


def marketplace_public_keys() -> dict[str, dict]:
    """Marketplace ID → public key, for verifying carts."""
    return {"mkt_paymart": slips.public_jwk(marketplace())}
