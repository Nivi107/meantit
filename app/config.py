"""Settings read from environment variables.

Locally they come from meantit/.env. On Render they are set in the dashboard
(see render.yaml).
"""

import os
from pathlib import Path

from dotenv import load_dotenv

BACKEND_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(BACKEND_ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


PAYPAL_BASE_URL = _env("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com")
PAYPAL_PAYMART_CLIENT_ID = _env("PAYPAL_PAYMART_CLIENT_ID")
PAYPAL_PAYMART_SECRET = _env("PAYPAL_PAYMART_SECRET")
PAYPAL_FUND_CLIENT_ID = _env("PAYPAL_FUND_CLIENT_ID")
PAYPAL_FUND_SECRET = _env("PAYPAL_FUND_SECRET")

DATABASE_URL = _env("DATABASE_URL")
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")

# Origins allowed to call the API from a browser. Local Vite dev server is always allowed.
FRONTEND_URL = _env("FRONTEND_URL")
CORS_ORIGINS = ["http://localhost:5173"]
if FRONTEND_URL:
    origin = FRONTEND_URL if FRONTEND_URL.startswith("http") else f"https://{FRONTEND_URL}"
    CORS_ORIGINS.append(origin.rstrip("/"))
