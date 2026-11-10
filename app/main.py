import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import agent, claims, config, dashboard, db, orders, store, webhooks

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("meantit")

EXPIRE_EVERY = 300  # seconds between sweeps for unanswered holds


async def expire_holds_forever():
    """Void HOLDs nobody answered within 24 hours. GET /orders also does this, in case the server slept."""
    while True:
        await asyncio.sleep(EXPIRE_EVERY)
        try:
            expired = await asyncio.to_thread(orders.expire_holds)
            if expired:
                log.info("voided %d expired hold(s)", expired)
        except Exception:  # noqa: BLE001  (keep sweeping)
            log.exception("expiring holds failed")


@asynccontextmanager
async def lifespan(app):
    db.init()
    sweeper = asyncio.create_task(expire_holds_forever())
    yield
    sweeper.cancel()
    with suppress(asyncio.CancelledError):
        await sweeper


app = FastAPI(
    lifespan=lifespan,
    title="MeantIt API",
    description="Checkout guard for AI agent orders, built on PayPal.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(webhooks.router)
app.include_router(store.router)
app.include_router(orders.router)
app.include_router(agent.router)
app.include_router(dashboard.router)
app.include_router(claims.router)


@app.get("/health")
def health():
    """Used by the frontend, Render and uptime pingers. Never returns secret values."""
    return {
        "status": "ok",
        "service": "meantit-api",
        "configured": {
            "paypal_paymart": bool(config.PAYPAL_PAYMART_CLIENT_ID and config.PAYPAL_PAYMART_SECRET),
            "paypal_fund": bool(config.PAYPAL_FUND_CLIENT_ID and config.PAYPAL_FUND_SECRET),
            "paypal_webhooks": bool(config.PAYPAL_PAYMART_WEBHOOK_ID and config.PAYPAL_FUND_WEBHOOK_ID),
            "signing_keys": bool(config.MARKETPLACE_SIGNING_KEY and config.DEMO_AGENT_SIGNING_KEY
                                 and config.CHECK_SIGNING_KEY),
            "database": bool(config.DATABASE_URL),
            "anthropic": bool(config.ANTHROPIC_API_KEY),
        },
    }
