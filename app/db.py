"""Database tables and connection (Neon Postgres in production, SQLite in tests).

Products and sellers stay in store_data.json; they are demo seed data that
never changes. Signed slips and Check records are never edited after saving.
"""

from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, Column, DateTime, Float, ForeignKey, Integer, MetaData, String, Table, Text,
                        create_engine, insert)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

from app import config

metadata = MetaData()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


users = Table(
    "users", metadata,
    Column("id", String, primary_key=True),
    Column("name", String, nullable=False),
    Column("public_jwk", JSON),          # registered from the browser
    Column("vault_id", String),          # saved PayPal account (PayMart's vault)
    Column("created_at", DateTime(timezone=True), default=utcnow),
)

orders = Table(
    "orders", metadata,
    Column("id", String, primary_key=True),
    Column("checkout_id", String, nullable=False),
    Column("checkout_hash", String, unique=True, nullable=False),  # same cart twice → same order
    Column("user_id", String, ForeignKey("users.id")),
    Column("seller_id", String),
    Column("total", Integer),             # cents
    Column("currency", String),
    Column("intent", Text),               # the user's own words
    Column("cart", JSON),                 # the signed cart's contents, for display
    Column("paypal_order_id", String),
    Column("authorization_id", String),
    Column("capture_id", String),
    Column("payment_status", String, default="CREATED"),  # CREATED, AUTHORIZED, CAPTURED, VOIDED, REFUNDED
    Column("decision", String, default="PENDING"),        # PENDING, ALLOW, HOLD, BLOCK
    Column("reason_codes", JSON, default=list),
    Column("hold_expires_at", DateTime(timezone=True)),
    Column("resolution", String),         # after a HOLD: APPROVED, REJECTED or EXPIRED
    Column("created_at", DateTime(timezone=True), default=utcnow),
    Column("updated_at", DateTime(timezone=True), default=utcnow, onupdate=utcnow),
)

mandates = Table(
    "mandates", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("order_id", String, ForeignKey("orders.id"), nullable=False),
    Column("kind", String, nullable=False),  # shopping_slip, money_slip, cart, final_ok
    Column("jwt", Text, nullable=False),
    Column("hash", String, nullable=False),
    Column("created_at", DateTime(timezone=True), default=utcnow),
)

checks = Table(
    "checks", metadata,
    Column("id", String, primary_key=True),
    Column("order_id", String, ForeignKey("orders.id"), nullable=False),
    Column("decision", String, nullable=False),
    Column("reason_codes", JSON, default=list),
    Column("checklist", JSON),
    Column("findings", JSON),
    Column("meaning", JSON),     # every meaning verdict, met ones included
    Column("input_hash", String),
    Column("prompt_version", String),
    Column("models", JSON),
    Column("record_jwt", Text),  # the whole Check record, signed by MeantIt
    Column("created_at", DateTime(timezone=True), default=utcnow),
)

webhook_events = Table(
    "webhook_events", metadata,
    Column("id", String, primary_key=True),  # PayPal's event ID, so a redelivery is ignored
    Column("app", String, nullable=False),
    Column("event_type", String),
    Column("resource_id", String),
    Column("status", String),
    Column("payload", JSON),
    Column("received_at", DateTime(timezone=True), default=utcnow),
)

agent_runs = Table(
    "agent_runs", metadata,
    Column("id", String, primary_key=True),
    Column("user_id", String, ForeignKey("users.id")),
    Column("mode", String),
    Column("request", Text),
    Column("status", String),            # running, done, failed
    Column("steps", JSON),               # what the agent did, for the progress screen
    Column("replayed", Boolean, default=False),  # replayed a saved run instead of asking Claude
    Column("cost_usd", Float, default=0.0),
    Column("order_id", String, ForeignKey("orders.id")),
    Column("error", Text),
    Column("created_at", DateTime(timezone=True), default=utcnow),
    Column("updated_at", DateTime(timezone=True), default=utcnow, onupdate=utcnow),
)

agent_plans = Table(
    "agent_plans", metadata,
    Column("key", String, primary_key=True),  # hash of mode, request, budget, prompt version
    Column("steps", JSON),
    Column("lines", JSON),                    # the cart the agent chose
    Column("created_at", DateTime(timezone=True), default=utcnow),
)

claims = Table(
    "claims", metadata,
    Column("id", String, primary_key=True),
    Column("order_id", String, ForeignKey("orders.id"), unique=True, nullable=False),  # one claim per order
    Column("user_id", String, ForeignKey("users.id")),
    Column("text", Text),                 # the shopper's own words
    Column("status", String),             # WITH_PAYMART, then PayMart's or the fund's decision (app/claims.py)
    Column("status_reason", Text),
    Column("verdict", JSON),              # the judge's answer, as returned
    Column("judge_model", String),
    Column("reviewed_by", String),        # "judge", or "admin" after manual review (fund claims only)
    Column("losses", JSON),               # what PayMart lost, item by item, in cents
    Column("refund_id", String),
    Column("payout_batch_id", String),
    Column("payout_status", String),
    Column("reimbursed", Integer),        # cents
    Column("created_at", DateTime(timezone=True), default=utcnow),
    Column("updated_at", DateTime(timezone=True), default=utcnow, onupdate=utcnow),
)

fund_ledger = Table(
    "fund_ledger", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ref", String, unique=True, nullable=False),  # e.g. "contribution:ord_x", so nothing is counted twice
    Column("kind", String, nullable=False),               # reserve, contribution, reimbursement
    Column("amount", Integer, nullable=False),            # cents: + in, - out
    Column("order_id", String),
    Column("claim_id", String),
    Column("paypal_ref", String),
    Column("note", Text),
    Column("created_at", DateTime(timezone=True), default=utcnow),
)

used_nonces = Table(
    "used_nonces", metadata,
    Column("nonce", String, primary_key=True),
    Column("used_at", Integer, nullable=False),
)


# ---- connection ----

engine = None


def configure(url: str):
    """Point the app at a database. Neon gives postgresql://; SQLAlchemy wants the driver named."""
    global engine
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    if url.startswith("sqlite") and ":memory:" in url:
        engine = create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    elif url.startswith("postgresql"):
        # pre_ping: Neon closes idle connections when it sleeps; reconnect instead of failing.
        # Keepalives and tcp_user_timeout: a connection a flaky network dropped silently fails in
        # seconds instead of hanging a request for minutes. recycle: don't keep any for long.
        engine = create_engine(url, pool_pre_ping=True, pool_recycle=300, connect_args={
            "connect_timeout": 10, "keepalives": 1, "keepalives_idle": 20, "keepalives_interval": 5,
            "keepalives_count": 3, "tcp_user_timeout": 15_000})
    else:
        engine = create_engine(url)
    return engine


def init():
    """Create any missing tables. Safe to run on every start."""
    metadata.create_all(engine)


configure(config.DATABASE_URL or "sqlite:///meantit-dev.db")


class NonceStore:
    """Used nonces, kept in the database so a replay is caught even after a restart."""

    def use(self, nonce: str, now: int) -> bool:
        try:
            with engine.begin() as conn:
                conn.execute(insert(used_nonces).values(nonce=nonce, used_at=now))
            return True
        except IntegrityError:
            return False
