"""Database tables and connection (Neon Postgres in production, SQLite in tests).

Products and sellers stay in store_data.json; they are demo seed data that
never changes. Signed slips and Check records are never edited after saving.
"""

from datetime import datetime, timezone

from sqlalchemy import (JSON, Column, DateTime, ForeignKey, Integer, MetaData, String, Table, Text,
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
    else:
        # pre_ping: Neon closes idle connections when it sleeps; reconnect instead of failing.
        engine = create_engine(url, pool_pre_ping=True)
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
