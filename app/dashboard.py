"""The marketplace's view of agent orders, and the evidence behind each decision.

GET /keys is MeantIt's published key list. With it, anyone can check an order's
evidence without trusting us: the browser re-checks every signature and hash.
"""

from fastapi import APIRouter
from sqlalchemy import select

from app import db, keys, orders, slips, store

router = APIRouter(tags=["dashboard"])

PIECES = ["shopping_slip", "money_slip", "cart", "final_ok"]


@router.get("/keys")
def public_keys():
    """Public keys for checking signatures: PayMart signs carts, MeantIt signs Check records."""
    return {
        "marketplaces": {mid: {"name": store.MARKETPLACE["name"], "public_jwk": jwk}
                         for mid, jwk in keys.marketplace_public_keys().items()},
        "meantit": {"public_jwk": slips.public_jwk(keys.check())},
    }


@router.get("/dashboard/orders")
def dashboard_orders(limit: int = 200):
    """Every agent order PayMart received, newest first, with which demo agent placed it."""
    rows = orders.list_orders(limit=limit)
    ids = [row["id"] for row in rows]
    with db.engine.connect() as conn:
        modes = {run.order_id: run.mode for run in conn.execute(
            select(db.agent_runs.c.order_id, db.agent_runs.c.mode).where(db.agent_runs.c.order_id.in_(ids)))} if ids else {}
    return [{**row, "demo_agent": modes.get(row["id"])} for row in rows]


@router.get("/orders/{order_id}/evidence")
def evidence(order_id: str):
    """The four signed pieces as received, the signed Check record, and the shopper's public key."""
    order = orders.order_view(order_id)
    with db.engine.connect() as conn:
        saved = {row.kind: row.jwt for row in conn.execute(
            select(db.mandates.c.kind, db.mandates.c.jwt).where(db.mandates.c.order_id == order_id))}
    user = orders._user(order["user_id"])
    return {
        "order_id": order_id,
        "decision": order["decision"],
        "pieces": {kind: saved.get(kind) for kind in PIECES},
        "check_record": order["check_record"],
        "shopper": {"id": user["id"], "public_jwk": user["public_jwk"]},
    }
