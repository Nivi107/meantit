"""PayPal webhooks: PayPal tells us when money moves (held, captured, voided, refunded).

Each sandbox app has its own webhook ID, so each gets its own URL:
/webhooks/paypal/paymart and /webhooks/paypal/fund.
"""

import logging
from collections import deque

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app import paypal

log = logging.getLogger("meantit.webhooks")
router = APIRouter(prefix="/webhooks/paypal", tags=["webhooks"])

CLIENTS = {"paymart": paypal.paymart, "fund": paypal.fund}

# Last verified events, newest first. Kept in memory until the database exists.
recent = deque(maxlen=50)


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

    # PayPal can deliver the same event more than once.
    if any(seen["id"] == event.get("id") for seen in recent):
        return {"status": "duplicate"}

    resource = event.get("resource", {})
    summary = {
        "id": event.get("id"),
        "app": app_name,
        "event_type": event.get("event_type"),
        "resource_id": resource.get("id"),
        "status": resource.get("status"),
        "created": event.get("create_time"),
    }
    recent.appendleft(summary)
    log.info("webhook %s: %s %s %s", app_name, summary["event_type"], summary["resource_id"], summary["status"])
    return {"status": "received"}


@router.get("/recent")
def recent_events():
    """Verified events since the server started (IDs and statuses only)."""
    return list(recent)
