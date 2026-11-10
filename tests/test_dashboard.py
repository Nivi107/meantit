"""The marketplace dashboard and the evidence anyone can re-check."""

from fastapi.testclient import TestClient

from app import slips
from app.main import app
from test_guard import FakeAI, place_order, use_ai, fake_paypal  # noqa: F401  (fixture)

client = TestClient(app)


def test_evidence_checks_out_with_the_published_keys(monkeypatch, fake_paypal):
    use_ai(monkeypatch, FakeAI())
    order = place_order("p01")[1].json()
    proof = client.get(f"/orders/{order['id']}/evidence").json()
    published = client.get("/keys").json()
    pieces = proof["pieces"]

    shopper = proof["shopper"]["public_jwk"]
    slips.read(pieces["shopping_slip"], shopper)
    slips.read(pieces["money_slip"], shopper)
    slips.read(pieces["cart"], published["marketplaces"]["mkt_paymart"]["public_jwk"])
    agent_key = slips.read_unverified(pieces["shopping_slip"])["cnf"]["jwk"]
    final_ok = slips.read(pieces["final_ok"], agent_key)
    record = slips.read(proof["check_record"], published["meantit"]["public_jwk"])

    assert final_ok["checkout_hash"] == slips.hash_of(pieces["cart"])
    assert final_ok["sd_hash"] == slips.hash_of(pieces["shopping_slip"])
    assert record["input_hashes"] == {kind: slips.hash_of(token) for kind, token in pieces.items()}
    assert record["decision"] == proof["decision"] == "ALLOW"
    assert "d" not in shopper and all("d" not in k["public_jwk"] for k in published["marketplaces"].values())


def test_dashboard_lists_orders_newest_first(monkeypatch, fake_paypal):
    use_ai(monkeypatch, FakeAI())
    first = place_order("p01")[1].json()
    second = place_order("p11")[1].json()
    rows = client.get("/dashboard/orders").json()
    ids = [row["id"] for row in rows]
    assert ids.index(second["id"]) < ids.index(first["id"])
    assert rows[0]["demo_agent"] is None  # placed straight through the guard, not by a demo agent


def test_evidence_for_a_missing_order():
    assert client.get("/orders/ord_nope/evidence").status_code == 404
