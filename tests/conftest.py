import json

import pytest

from app import config, db, slips
from app.guard import ai

# Fresh signing keys for every test run, so tests never depend on .env.
TEST_KEYS = {name: json.dumps(slips.new_private_jwk())
             for name in ("MARKETPLACE_SIGNING_KEY", "DEMO_AGENT_SIGNING_KEY", "CHECK_SIGNING_KEY")}


@pytest.fixture(autouse=True)
def signing_keys(monkeypatch):
    for name, value in TEST_KEYS.items():
        monkeypatch.setattr(config, name, value)


@pytest.fixture(autouse=True)
def database():
    """A fresh, empty in-memory database for every test."""
    db.configure("sqlite:///:memory:")
    db.init()
    yield
    db.engine.dispose()


@pytest.fixture(autouse=True)
def no_claude(monkeypatch):
    """Tests never call the real Claude API (it costs money), even with a key in .env."""
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(ai, "_default", None)
