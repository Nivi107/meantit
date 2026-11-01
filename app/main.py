import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import config, webhooks

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

app = FastAPI(
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
            "database": bool(config.DATABASE_URL),
            "anthropic": bool(config.ANTHROPIC_API_KEY),
        },
    }
