"""The Protection Fund and claims (design doc, Module 6): the second layer.

A shopper reports a problem to PayMart, the way returns work at any shop. PayMart
then accepts a normal return under its own policy, declines, or sends the claim to
the MeantIt Protection Fund when it believes the agent got it wrong. That hands the
claim over: PayMart's click returns at once, and MeantIt checks it on its own side
afterwards. PayMart and the shopper see only the outcome; the judge's reasoning
stays in MeantIt's admin console. When the fund covers it, the shopper returns the item, PayMart
refunds the shopper, and the fund repays PayMart's losses through PayPal Payouts.
Nobody ends up with both the item and the money.

Code checks coverage first. The claim judge (Claude) only gives a verdict; code
applies the rules and moves the money. A judge that is unsure, or unavailable,
sends the claim to a person (the admin's review queue).
"""

import logging
import secrets
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError

from app import db, orders, paypal, store
from app.guard import ai as guard_ai
from app.guard import policy

log = logging.getLogger("meantit.claims")
router = APIRouter(tags=["protection fund and claims"])

CONTRIBUTION_RATE = 0.01        # 1% of every captured guarded order goes into the fund
STARTING_RESERVE = 25000        # cents; the sandbox fund's opening balance
RETURN_SHIPPING = 800           # cents; PayMart's flat return label cost in the demo
CONFIDENCE_MIN = 0.7            # below this the judge's verdict goes to a person
REPEAT_CLAIMS = 2               # this many earlier claims in REPEAT_DAYS sends a new one to review
REPEAT_DAYS = 30

# PayMart's own decisions
WITH_PAYMART, STORE_RETURN, STORE_REFUNDED, STORE_DECLINED = (
    "WITH_PAYMART", "STORE_RETURN", "STORE_REFUNDED", "STORE_DECLINED")
# The fund's decisions, once PayMart sends a claim to MeantIt (SENT_TO_FUND: MeantIt hasn't answered yet)
SENT_TO_FUND = "SENT_TO_FUND"
NOT_COVERED, IN_REVIEW, DECLINED = "NOT_COVERED", "IN_REVIEW", "DECLINED"
APPROVED, REFUNDED, REIMBURSED = "APPROVED", "REFUNDED", "REIMBURSED"
# Where PayMart can still accept or decline under its own policy: before the fund, or after the fund said no
STORE_CAN_DECIDE = {WITH_PAYMART, NOT_COVERED, DECLINED}


def get_ai():
    """The judge: Claude if ANTHROPIC_API_KEY is set. Tests swap in a fake."""
    return guard_ai.default_ai()


# ---- the shopper reports a problem to PayMart ----

class ClaimRequest(BaseModel):
    text: str = Field(min_length=10, max_length=2000)


@router.post("/orders/{order_id}/claims")
def file_claim(order_id: str, body: ClaimRequest, x_user_id: str | None = Header(None)):
    """No AI here: the claim goes to PayMart first."""
    order = orders._order(order_id)
    if x_user_id and x_user_id != order["user_id"]:
        raise HTTPException(403, "Not your order")
    if order["payment_status"] != "CAPTURED":
        raise HTTPException(409, "Only a paid order can have a claim")

    claim_id = "clm_" + secrets.token_hex(6)
    try:
        with db.engine.begin() as conn:
            conn.execute(insert(db.claims).values(id=claim_id, order_id=order_id, user_id=order["user_id"],
                                                  text=body.text, status=WITH_PAYMART,
                                                  status_reason="Sent to PayMart. They'll answer here."))
    except IntegrityError:
        raise HTTPException(409, "This order already has a claim")
    return claim_view(claim_id)


# ---- PayMart decides: its own return, a no, or the Protection Fund ----

class MerchantDecision(BaseModel):
    action: Literal["accept_return", "decline", "send_to_fund"]
    note: str = Field("", max_length=500)


@router.post("/claims/{claim_id}/merchant")
def merchant_decides(claim_id: str, body: MerchantDecision, background: BackgroundTasks):
    claim = _claim(claim_id)
    note = f" PayMart's note: {body.note.strip()}" if body.note.strip() else ""
    if body.action == "send_to_fund":
        # Only one click wins, so the judge runs once
        if not _move(claim_id, WITH_PAYMART, status=SENT_TO_FUND, status_reason=(
                "PayMart sent this to the MeantIt Protection Fund. MeantIt will answer here." + note)):
            raise HTTPException(409, "Only a new claim can go to the Protection Fund")
        background.add_task(_fund_decides, claim_id)
        return claim_view(claim_id)
    if claim["status"] not in STORE_CAN_DECIDE:
        raise HTTPException(409, "This claim is past PayMart's decision")
    if body.action == "accept_return":
        _update(claim_id, status=STORE_RETURN, status_reason=(
            "PayMart accepted your return under its own return policy. Send it back for a full refund." + note))
    else:
        _update(claim_id, status=STORE_DECLINED, status_reason=(
            "PayMart declined the return." + note + " If the item isn't as described, PayPal's buyer protection "
            "still applies."))
    return claim_view(claim_id)


def _fund_decides(claim_id: str):
    """MeantIt's turn, after PayMart's request has returned: coverage rules in code first, then the claim judge.
    Saves only if the claim is still waiting, so a person who decided meanwhile isn't overwritten."""
    claim = _claim(claim_id)
    order = orders._order(claim["order_id"])
    status, reason = _coverage(order)
    if status:
        _move(claim_id, SENT_TO_FUND, status=status, status_reason=reason)
        return
    status, reason, verdict, model = _judge(order, claim["text"])
    _move(claim_id, SENT_TO_FUND, status=status, status_reason=reason, verdict=verdict, judge_model=model,
          reviewed_by="judge")


def _coverage(order: dict) -> tuple[str | None, str | None]:
    """The rules code checks before any AI. Returns (status, reason) to stop, or (None, None) to judge."""
    if order["decision"] == policy.HOLD and order["resolution"] == "APPROVED":
        return NOT_COVERED, ("MeantIt held this order and you approved it anyway, so the fund doesn't cover it. "
                             "PayMart decides on a normal return.")
    if order["decision"] != policy.ALLOW:
        return NOT_COVERED, "MeantIt didn't approve this order, so the fund doesn't cover it."
    if not order["intent"]:
        return NOT_COVERED, "There's no signed request with this order, so nobody can prove what you meant."
    since = db.utcnow() - timedelta(days=REPEAT_DAYS)
    with db.engine.connect() as conn:
        earlier = conn.execute(select(func.count()).select_from(db.claims).where(
            db.claims.c.user_id == order["user_id"], db.claims.c.order_id != order["id"],
            db.claims.c.created_at >= since)).scalar()
    if earlier >= REPEAT_CLAIMS:
        return IN_REVIEW, f"This shopper filed {earlier} other claims in {REPEAT_DAYS} days, so a person checks it."
    return None, None


def _judge(order: dict, text: str) -> tuple[str, str, dict | None, str | None]:
    judge = get_ai()
    try:
        verdict = judge.judge(_case(order, text))
    except guard_ai.AIUnavailable as err:
        log.warning("claim on %s: judge unavailable: %s", order["id"], err)
        return IN_REVIEW, "The claim judge couldn't answer, so a person checks it.", None, None
    model = getattr(judge, "models", {}).get("judge")
    answer = verdict.model_dump()
    sure = verdict.confidence >= CONFIDENCE_MIN
    if verdict.verdict == "NOT_RECEIVED":
        return NOT_COVERED, ("This sounds like an item that didn't arrive or a payment you didn't authorize. "
                             "PayPal Seller Protection and PayPal's dispute process handle those, so the fund "
                             "doesn't pay twice."), answer, model
    if verdict.verdict == "SUSPICIOUS" or not sure:
        return IN_REVIEW, "The judge wasn't confident, so a person checks it.", answer, model
    if verdict.verdict == "CHANGED_MIND":
        return DECLINED, ("What the agent bought matches what you asked for, so this isn't an agent mistake. "
                          "PayMart decides on a normal return."), answer, model
    return APPROVED, "The agent bought something you didn't mean. Send it back; PayMart refunds you in full.", answer, model


def _case(order: dict, text: str) -> dict:
    """Everything the judge sees. All of it is data, passed inside tags."""
    check = orders.order_view(order["id"])
    bought, later = [], []
    for item in check["items"]:
        product = store.PRODUCTS.get(item.get("product_id"), {})
        bought.append({"title": item["title"], "qty": item["qty"], "description": product.get("description"),
                       "attributes": product.get("attributes")})
        later += [{"product": product.get("name"), **review} for review in product.get("later_reviews", [])]
    return {
        "signed_request": order["intent"],
        "bought": bought,
        "seller": check["seller"]["name"],
        "guard_at_checkout": {"decision": check["decision"], "meaning": check["meaning"]},
        "published_after_checkout": later,
        "shopper_claim": text,
    }


# ---- PayMart gets the item back: refund the shopper, reimburse PayMart ----

class ReturnReceived(BaseModel):
    resellable: bool = True  # if it can't be sold again, the item's value is a loss too


@router.post("/claims/{claim_id}/return-received")
def return_received(claim_id: str, body: ReturnReceived = ReturnReceived()):
    claim = _claim(claim_id)
    if claim["status"] == REFUNDED:
        return _reimburse(claim_id)  # the refund went through earlier; try the payout again
    if claim["status"] not in (APPROVED, STORE_RETURN):
        raise HTTPException(409, "This claim isn't waiting for a return")
    order = orders._order(claim["order_id"])
    client = paypal.paymart()

    refund = client.refund_capture(order["capture_id"], request_id=f"refund-{claim_id}")
    if claim["status"] == STORE_RETURN:  # PayMart's own return: it carries the costs, the fund isn't involved
        orders._update(order["id"], payment_status="REFUNDED")
        _update(claim_id, status=STORE_REFUNDED, refund_id=refund.get("id"),
                status_reason="PayMart refunded you in full under its return policy.")
        return claim_view(claim_id)
    capture = client.get_capture(order["capture_id"])
    fee = _cents(capture.get("seller_receivable_breakdown", {}).get("paypal_fee", {}).get("value"))
    losses = {"return_shipping": RETURN_SHIPPING, "paypal_fee": fee,
              "unsellable_item": 0 if body.resellable else order["total"]}
    orders._update(order["id"], payment_status="REFUNDED")
    _update(claim_id, status=REFUNDED, refund_id=refund.get("id"), losses=losses,
            status_reason="PayMart refunded you in full.")
    return _reimburse(claim_id, payee=capture.get("payee", {}).get("merchant_id"))


def _reimburse(claim_id: str, payee: str | None = None) -> dict:
    """The fund pays PayMart's documented losses, never more than the order or the fund's balance."""
    claim = _claim(claim_id)
    order = orders._order(claim["order_id"])
    if not payee:
        payee = paypal.paymart().get_capture(order["capture_id"]).get("payee", {}).get("merchant_id")
    amount = min(sum(claim["losses"].values()), order["total"], fund_balance())
    if amount <= 0:
        _update(claim_id, status_reason="Refunded. The fund is empty, so PayMart's costs wait for a top-up.")
        return claim_view(claim_id)
    batch_id = f"reimburse-{claim_id}"
    try:
        payout = paypal.fund().create_payout(payee, amount, batch_id=batch_id,
                                             note=f"MeantIt Protection Fund: claim {claim_id} on {order['id']}")
    except paypal.PayPalError as err:
        log.error("claim %s: payout failed: %s", claim_id, err)
        _update(claim_id, status_reason="Refunded. The fund's payout to PayMart failed; it can be retried.")
        return claim_view(claim_id)
    header = payout.get("batch_header", {})
    _ledger(f"reimbursement:{claim_id}", "reimbursement", -amount, order_id=order["id"], claim_id=claim_id,
            paypal_ref=header.get("payout_batch_id"), note="Reimbursed PayMart's losses")
    _update(claim_id, status=REIMBURSED, reimbursed=amount, payout_batch_id=header.get("payout_batch_id"),
            payout_status=header.get("batch_status"),
            status_reason="PayMart refunded you in full, and the Protection Fund repaid PayMart's costs.")
    return claim_view(claim_id)


# ---- the admin decides claims the judge wasn't sure about ----

class Review(BaseModel):
    approve: bool
    note: str = ""


@router.post("/claims/{claim_id}/review")
def review(claim_id: str, body: Review):
    claim = _claim(claim_id)
    if claim["status"] not in (IN_REVIEW, SENT_TO_FUND):  # SENT_TO_FUND: the judge is stuck, a person decides
        raise HTTPException(409, "This claim isn't waiting for review")
    if body.approve:
        status, reason = APPROVED, "A person reviewed it: the agent made a mistake. Send it back for a full refund."
    else:
        status, reason = DECLINED, "A person reviewed it and it isn't covered. PayMart decides on a normal return."
    if body.note:
        reason += f" Note: {body.note}"
    if not _move(claim_id, claim["status"], status=status, status_reason=reason, reviewed_by="admin"):
        raise HTTPException(409, "The judge decided this claim a moment ago")
    return claim_view(claim_id)


# ---- reading ----

@router.get("/claims")
def list_claims(user_id: str | None = None):
    query = select(db.claims).order_by(db.claims.c.created_at.desc())
    if user_id:
        query = query.where(db.claims.c.user_id == user_id)
    # One connection for both reads: the database is a network round trip away
    with db.engine.connect() as conn:
        rows = [dict(row._mapping) for row in conn.execute(query)]
        ids = [row["order_id"] for row in rows]
        totals = {row.id: row for row in conn.execute(
            select(db.orders.c.id, db.orders.c.total, db.orders.c.intent, db.orders.c.cart)
            .where(db.orders.c.id.in_(ids)))} if ids else {}
    return [_view(_refresh_payout(row), totals.get(row["order_id"])) for row in rows]


@router.get("/claims/{claim_id}")
def claim_view(claim_id: str):
    claim = _refresh_payout(_claim(claim_id))
    return _view(claim, orders._order(claim["order_id"]))


@router.get("/fund")
def fund():
    """The fund's balance and every movement in and out."""
    _sync_ledger()
    with db.engine.connect() as conn:
        rows = [dict(row._mapping) for row in conn.execute(select(db.fund_ledger).order_by(db.fund_ledger.c.id))]
    money_in = sum(r["amount"] for r in rows if r["amount"] > 0)
    money_out = -sum(r["amount"] for r in rows if r["amount"] < 0)
    return {
        "balance": money_in - money_out,
        "money_in": money_in,
        "money_out": money_out,
        "contribution_rate": CONTRIBUTION_RATE,
        "ledger": [{**r, "created_at": orders._iso(r["created_at"])} for r in reversed(rows)],
    }


def _refresh_payout(claim: dict) -> dict:
    """Payouts finish a few seconds after they're sent; ask PayPal until it says how it ended."""
    if claim["payout_batch_id"] and claim["payout_status"] in ("PENDING", "PROCESSING"):
        try:
            status = paypal.fund().get_payout(claim["payout_batch_id"])["batch_header"]["batch_status"]
        except paypal.PayPalError as err:
            log.warning("claim %s: payout status unknown: %s", claim["id"], err)
            return claim
        if status != claim["payout_status"]:
            _update(claim["id"], payout_status=status)
            claim = {**claim, "payout_status": status}
    return claim


def fund_balance() -> int:
    _sync_ledger()
    with db.engine.connect() as conn:
        return conn.execute(select(func.coalesce(func.sum(db.fund_ledger.c.amount), 0))).scalar()


def _sync_ledger():
    """Add the opening reserve and a contribution for every captured order not yet counted."""
    t = db.orders
    with db.engine.connect() as conn:
        counted = set(conn.execute(select(db.fund_ledger.c.ref)).scalars())
        paid = conn.execute(select(t.c.id, t.c.total, t.c.capture_id, t.c.created_at)
                            .where(t.c.capture_id.isnot(None))).all()
    if "reserve:start" not in counted:
        _ledger("reserve:start", "reserve", STARTING_RESERVE, note="Opening reserve (sandbox funds)")
    for row in paid:
        ref = f"contribution:{row.id}"
        if ref not in counted:
            _ledger(ref, "contribution", round(row.total * CONTRIBUTION_RATE), order_id=row.id,
                    paypal_ref=row.capture_id, note="1% of a captured guarded order", created_at=row.created_at)


def _ledger(ref, kind, amount, **values):
    try:
        with db.engine.begin() as conn:
            conn.execute(insert(db.fund_ledger).values(ref=ref, kind=kind, amount=amount, **values))
    except IntegrityError:
        pass  # already recorded (two requests at once)


# ---- helpers ----

def _view(claim: dict, order) -> dict:
    order = dict(order._mapping) if hasattr(order, "_mapping") else (order or {})
    cart = order.get("cart") or {}
    return {
        "id": claim["id"],
        "order_id": claim["order_id"],
        "user_id": claim["user_id"],
        "text": claim["text"],
        "status": claim["status"],
        "status_reason": claim["status_reason"],
        "verdict": claim["verdict"],
        "judge_model": claim["judge_model"],
        "reviewed_by": claim["reviewed_by"],
        "losses": claim["losses"],
        "refund_id": claim["refund_id"],
        "payout_batch_id": claim["payout_batch_id"],
        "payout_status": claim["payout_status"],
        "reimbursed": claim["reimbursed"],
        "order_total": order.get("total"),
        "intent": order.get("intent"),
        "items": cart.get("items", []),
        "created_at": orders._iso(claim["created_at"]),
        "updated_at": orders._iso(claim["updated_at"]),
    }


def _claim(claim_id: str) -> dict:
    with db.engine.connect() as conn:
        row = conn.execute(select(db.claims).where(db.claims.c.id == claim_id)).first()
    if not row:
        raise HTTPException(404, f"No claim {claim_id!r}")
    return dict(row._mapping)


def _update(claim_id: str, **values):
    with db.engine.begin() as conn:
        conn.execute(update(db.claims).where(db.claims.c.id == claim_id).values(**values))


def _move(claim_id: str, expected: str, **values) -> bool:
    """Update the claim only if it's still in `expected`. False if someone else moved it first."""
    with db.engine.begin() as conn:
        result = conn.execute(update(db.claims).where(db.claims.c.id == claim_id, db.claims.c.status == expected)
                              .values(**values))
    return result.rowcount == 1


def _cents(value: str | None) -> int:
    if not value:
        return 0
    whole, _, part = value.partition(".")
    return int(whole) * 100 + int((part + "00")[:2])
