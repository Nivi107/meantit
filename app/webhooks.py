"""PayPal webhooks: PayPal tells us when money moves (held, captured, voided, refunded).

Each sandbox app has its own webhook ID, so each gets its own URL:
/webhooks/paypal/paymart and /webhooks/paypal/fund.
"""

import logging

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from app import db, paypal

log = logging.getLogger("meantit.webhooks")
router = APIRouter(prefix="/webhooks/paypal", tags=["webhooks"])

CLIENTS = {"paymart": paypal.paymart, "fund": paypal.fund}


@router.post("/{app_name}")
async def receive(app_name: str, request: Request):
    if app_name not in CLIENTS:
        raise HTTPException(404, "unknown app")
    event = await request.json()
    client = CLIENTS[app_name]()

    try:
        # verify_webhook makes a blocking HTTP call, so run it off the event loop.
        verified = await run_in_threadpool(client.verify_webhook, request.headers, event)
    except paypal.PayPalError as err:
        log.warning("webhook %s: PayPal verify call failed: %s", app_name, err)
        raise HTTPException(502, "could not verify with PayPal")  # non-2xx: PayPal retries
    if not verified:
        log.warning("webhook %s: signature not verified, event %s", app_name, event.get("id"))
        raise HTTPException(400, "signature not verified")

    resource = event.get("resource", {})
    row = {
        "id": event.get("id"),
        "app": app_name,
        "event_type": event.get("event_type"),
        "resource_id": resource.get("id"),
        "status": resource.get("status"),
        "payload": event,
    }
    try:
        with db.engine.begin() as conn:
            conn.execute(insert(db.webhook_events).values(**row))
    except IntegrityError:
        return {"status": "duplicate"}  # PayPal can deliver the same event more than once
    log.info("webhook %s: %s %s %s", app_name, row["event_type"], row["resource_id"], row["status"])
    return {"status": "received"}


@router.get("/recent")
def recent_events(limit: int = 50):
    """Latest verified events (IDs and statuses only)."""
    t = db.webhook_events
    query = (select(t.c.id, t.c.app, t.c.event_type, t.c.resource_id, t.c.status, t.c.received_at)
             .order_by(t.c.received_at.desc()).limit(min(limit, 200)))
    with db.engine.connect() as conn:
        return [dict(row._mapping) for row in conn.execute(query)]
