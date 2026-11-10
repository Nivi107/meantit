"""The guard endpoint and the orders it creates.

POST /guard/checks is the product: it takes the four signed pieces, holds the
money on PayPal (intent AUTHORIZE, saved account, no popup), runs the guard at
the same time, then captures (ALLOW), voids (BLOCK) or waits for the user (HOLD).
"""

import logging
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from app import config, db, keys, paypal, slips, store
from app.guard import ai as guard_ai
from app.guard import pipeline, policy
from app.guard.verify import verify

log = logging.getLogger("meantit.orders")
router = APIRouter(tags=["guard and orders"])

HOLD_HOURS = 24


def get_ai():
    """The AI the guard uses: Claude if ANTHROPIC_API_KEY is set. Tests swap in a fake."""
    return guard_ai.default_ai()


# ---- users (demo) ----

class KeyRegistration(BaseModel):
    name: str
    public_jwk: dict


@router.post("/users/{user_id}/keys")
def register_key(user_id: str, body: KeyRegistration):
    """The browser registers the public half of the user's signing key."""
    try:
        jwk = slips.public_jwk(body.public_jwk)
    except (ValueError, KeyError):
        raise HTTPException(422, "public_jwk must be an EC P-256 public key")
    with db.engine.begin() as conn:
        if conn.execute(select(db.users.c.id).where(db.users.c.id == user_id)).first():
            conn.execute(update(db.users).where(db.users.c.id == user_id).values(name=body.name, public_jwk=jwk))
        else:
            conn.execute(insert(db.users).values(id=user_id, name=body.name, public_jwk=jwk))
    return get_user(user_id)


@router.get("/users/{user_id}")
def get_user(user_id: str):
    user = _user(user_id)
    return {"id": user["id"], "name": user["name"], "has_key": bool(user["public_jwk"]),
            "paypal_connected": bool(user["vault_id"] or config.SANDBOX_VAULT_ID)}


def _user(user_id: str) -> dict:
    with db.engine.connect() as conn:
        row = conn.execute(select(db.users).where(db.users.c.id == user_id)).first()
    if not row:
        raise HTTPException(404, f"No user {user_id!r}")
    return dict(row._mapping)


# ---- the guard ----

class GuardRequest(BaseModel):
    user_id: str
    shopping_slip: str
    money_slip: str
    cart: str
    final_ok: str


@router.post("/guard/checks")
def guard_check(body: GuardRequest):
    user = _user(body.user_id)
    if not user["public_jwk"]:
        raise HTTPException(409, "This user hasn't registered a signing key")
    bundle = {"shopping_slip": body.shopping_slip, "money_slip": body.money_slip,
              "cart": body.cart, "final_ok": body.final_ok}
    checkout_hash = slips.hash_of(body.cart)

    # Idempotent: the same signed cart always returns the same order.
    existing = _order_id_by_hash(checkout_hash)
    if existing:
        return order_view(existing)

    order_id = "ord_" + secrets.token_hex(6)
    verified = verify(bundle, user["public_jwk"], keys.marketplace_public_keys(), db.NonceStore())

    if not verified.ok:
        # Stage 1 failed: BLOCK, and no money is ever held.
        outcome = pipeline.blocked_by_verify(verified)
        unique = f"invalid:{order_id}"  # don't take the cart's slot, so a valid retry still works
        _save_order(order_id, unique, user["id"], bundle, verified, outcome, payment_status="NONE")
        return order_view(order_id)

    vault_id = user["vault_id"] or config.SANDBOX_VAULT_ID
    if not vault_id:
        raise HTTPException(409, "This user hasn't connected PayPal yet")

    try:
        _save_order(order_id, checkout_hash, user["id"], bundle, verified, None, payment_status="CREATED")
    except IntegrityError:
        return order_view(_order_id_by_hash(checkout_hash))  # a duplicate request won the race

    # Hold the money and run the guard at the same time; act when both are done.
    with ThreadPoolExecutor(max_workers=2) as pool:
        hold_job = pool.submit(_hold_money, order_id, verified.cart, vault_id)
        guard_job = pool.submit(pipeline.run_checks, verified, get_ai())
        outcome = guard_job.result()
        try:
            hold = hold_job.result()
        except paypal.PayPalError as err:
            log.error("order %s: PayPal hold failed: %s", order_id, err)
            hold = None

    record = pipeline.sign_record(order_id, checkout_hash, bundle, outcome)
    changes = {"decision": outcome.decision, "reason_codes": outcome.reason_codes}
    if hold:
        changes.update(hold)
        changes.update(_act_on_decision(order_id, outcome.decision, hold["authorization_id"]))
    else:
        changes["payment_status"] = "FAILED"
    with db.engine.begin() as conn:
        conn.execute(update(db.orders).where(db.orders.c.id == order_id).values(**changes))
        conn.execute(insert(db.checks).values(_check_row(order_id, outcome, record)))
    return order_view(order_id)


def _hold_money(order_id: str, cart: dict, vault_id: str) -> dict:
    client = paypal.paymart()
    titles = ", ".join(f'{line["qty"]}× {line["title"]}' for line in cart["items"])
    order = client.create_order(cart["total"], currency=cart["currency"], description=f"PayMart: {titles}",
                                reference_id=cart["checkout_id"], vault_id=vault_id, request_id=f"hold-{order_id}")
    if order.get("status") == "APPROVED":
        order = client.authorize_order(order["id"], request_id=f"authorize-{order_id}")
    auth = paypal.first_authorization(order)
    if not auth:
        raise paypal.PayPalError(500, {"name": "NO_AUTHORIZATION", "message": f"order status {order.get('status')}"})
    return {"paypal_order_id": order["id"], "authorization_id": auth["id"], "payment_status": "AUTHORIZED"}


def _act_on_decision(order_id: str, decision: str, authorization_id: str) -> dict:
    client = paypal.paymart()
    try:
        if decision == policy.ALLOW:
            capture = client.capture_authorization(authorization_id, request_id=f"capture-{order_id}")
            return {"capture_id": capture["id"], "payment_status": "CAPTURED"}
        if decision == policy.BLOCK:
            client.void_authorization(authorization_id, request_id=f"void-{order_id}")
            return {"payment_status": "VOIDED"}
    except paypal.PayPalError as err:
        log.error("order %s: PayPal %s failed: %s", order_id, decision, err)
        return {}
    return {"hold_expires_at": db.utcnow() + timedelta(hours=HOLD_HOURS)}


# ---- orders ----

@router.get("/orders")
def list_orders(user_id: str | None = None, limit: int = 50):
    expire_holds()
    query = select(db.orders).order_by(db.orders.c.created_at.desc()).limit(min(limit, 200))
    if user_id:
        query = query.where(db.orders.c.user_id == user_id)
    # Two queries for the whole list: Neon is a network hop away, so one query per order adds up.
    with db.engine.connect() as conn:
        rows = [dict(row._mapping) for row in conn.execute(query)]
        ids = [row["id"] for row in rows]
        checks = {row.order_id: dict(row._mapping)
                  for row in conn.execute(select(db.checks).where(db.checks.c.order_id.in_(ids)))} if ids else {}
    return [_view(row, checks.get(row["id"], {})) for row in rows]


@router.get("/orders/{order_id}")
def get_order(order_id: str):
    expire_holds()
    return order_view(order_id)


@router.post("/orders/{order_id}/approve")
def approve(order_id: str, x_user_id: str | None = Header(None)):
    """The user says "yes, I meant this": capture the held money."""
    order = _held_order(order_id, x_user_id)
    capture = paypal.paymart().capture_authorization(order["authorization_id"], request_id=f"capture-{order_id}")
    _update(order_id, capture_id=capture["id"], payment_status="CAPTURED", resolution="APPROVED")
    return order_view(order_id)


@router.post("/orders/{order_id}/reject")
def reject(order_id: str, x_user_id: str | None = Header(None)):
    """The user says "no": void the hold, nothing is charged."""
    order = _held_order(order_id, x_user_id)
    paypal.paymart().void_authorization(order["authorization_id"], request_id=f"void-{order_id}")
    _update(order_id, payment_status="VOIDED", resolution="REJECTED")
    return order_view(order_id)


def expire_holds(now: datetime | None = None) -> int:
    """Void every HOLD nobody answered within 24 hours. Returns how many."""
    now = now or db.utcnow()
    t = db.orders
    query = select(t.c.id, t.c.authorization_id, t.c.hold_expires_at).where(
        t.c.decision == policy.HOLD, t.c.payment_status == "AUTHORIZED", t.c.resolution.is_(None))
    with db.engine.connect() as conn:
        due = [row for row in conn.execute(query) if row.hold_expires_at and _aware(row.hold_expires_at) <= now]
    for row in due:
        try:
            paypal.paymart().void_authorization(row.authorization_id, request_id=f"void-{row.id}")
            _update(row.id, payment_status="VOIDED", resolution="EXPIRED")
        except paypal.PayPalError as err:
            log.error("order %s: expiring hold failed: %s", row.id, err)
    return len(due)


def _held_order(order_id: str, user_id: str | None) -> dict:
    order = _order(order_id)
    if user_id and user_id != order["user_id"]:
        raise HTTPException(403, "Not your order")
    if order["decision"] != policy.HOLD or order["payment_status"] != "AUTHORIZED" or order["resolution"]:
        raise HTTPException(409, "This order isn't waiting for your answer")
    if order["hold_expires_at"] and _aware(order["hold_expires_at"]) <= db.utcnow():
        raise HTTPException(409, "This hold has expired")
    return order


def order_view(order_id: str) -> dict:
    order = _order(order_id)
    with db.engine.connect() as conn:
        check = conn.execute(select(db.checks).where(db.checks.c.order_id == order_id)).first()
    return _view(order, dict(check._mapping) if check else {})


def _view(order: dict, check: dict) -> dict:
    findings = check.get("findings") or []
    cart = order["cart"] or {}
    seller = store.SELLERS.get(order["seller_id"], {})
    return {
        "id": order["id"],
        "user_id": order["user_id"],
        "checkout_id": order["checkout_id"],
        "intent": order["intent"],
        "decision": order["decision"],
        "reason_codes": order["reason_codes"] or [],
        "reasons": [f["message"] for f in findings
                    if policy.DECISION_FOR.get(f["code"], policy.HOLD) != policy.ALLOW],
        "findings": findings,
        "meaning": check.get("meaning") or [],
        "checklist": check.get("checklist"),
        "payment_status": order["payment_status"],
        "resolution": order["resolution"],
        "hold_expires_at": _iso(order["hold_expires_at"]),
        "seller": {"id": order["seller_id"], "name": seller.get("name")},
        "items": cart.get("items", []),
        "total": order["total"],
        "currency": order["currency"],
        "delivery_estimate": cart.get("delivery_estimate"),
        "paypal": {"order_id": order["paypal_order_id"], "authorization_id": order["authorization_id"],
                   "capture_id": order["capture_id"]},
        "check_record": check.get("record_jwt"),
        "created_at": _iso(order["created_at"]),
    }


# ---- storage helpers ----

def _save_order(order_id, checkout_hash, user_id, bundle, verified, outcome, payment_status):
    cart = verified.cart or slips.read_unverified(bundle["cart"])
    row = {
        "id": order_id, "checkout_id": str(cart.get("checkout_id", "")), "checkout_hash": checkout_hash,
        "user_id": user_id, "seller_id": cart.get("seller_id"), "total": cart.get("total"),
        "currency": cart.get("currency"), "intent": verified.intent if verified.shopping_slip else None,
        "cart": cart, "payment_status": payment_status,
        "decision": outcome.decision if outcome else "PENDING",
        "reason_codes": outcome.reason_codes if outcome else [],
    }
    with db.engine.begin() as conn:
        conn.execute(insert(db.orders).values(**row))
        conn.execute(insert(db.mandates), [{"order_id": order_id, "kind": kind, "jwt": token,
                                            "hash": slips.hash_of(token)} for kind, token in bundle.items()])
        if outcome:
            record = pipeline.sign_record(order_id, checkout_hash, bundle, outcome)
            conn.execute(insert(db.checks).values(_check_row(order_id, outcome, record)))


def _check_row(order_id, outcome, record) -> dict:
    return {"id": "chk_" + secrets.token_hex(6), "order_id": order_id, "decision": outcome.decision,
            "reason_codes": outcome.reason_codes, "checklist": outcome.checklist,
            "findings": [f.to_dict() for f in outcome.findings], "meaning": outcome.meaning,
            "prompt_version": outcome.prompt_version, "models": outcome.models, "record_jwt": record}


def _order(order_id: str) -> dict:
    with db.engine.connect() as conn:
        row = conn.execute(select(db.orders).where(db.orders.c.id == order_id)).first()
    if not row:
        raise HTTPException(404, f"No order {order_id!r}")
    return dict(row._mapping)


def _order_id_by_hash(checkout_hash: str) -> str | None:
    with db.engine.connect() as conn:
        row = conn.execute(select(db.orders.c.id).where(db.orders.c.checkout_hash == checkout_hash)).first()
    return row.id if row else None


def _update(order_id: str, **values):
    with db.engine.begin() as conn:
        conn.execute(update(db.orders).where(db.orders.c.id == order_id).values(**values))


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)  # SQLite drops the timezone


def _iso(moment: datetime | None) -> str | None:
    return _aware(moment).isoformat() if moment else None
