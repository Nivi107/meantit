"""The demo shopping agent with a scripted fake Claude: no network, no cost."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import agent, config, keys, slips, store
from app.main import app
from app.guard.schemas import InjectionAsk
from test_guard import REQUEST, USER_KEY, FakeAI, register, use_ai, fake_paypal  # noqa: F401  (fixture)

client = TestClient(app)


def tool(name, **args):
    return SimpleNamespace(type="tool_use", id=f"tu_{name}_{len(args)}", name=name, input=args)


def say(text):
    return SimpleNamespace(type="text", text=text)


class ScriptedClaude:
    """Answers each turn with the next scripted list of blocks."""

    def __init__(self, turns, last_stop="tool_use"):
        self.turns, self.last_stop, self.requests = list(turns), last_stop, []
        self.messages = self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        blocks = self.turns.pop(0)
        stop = "tool_use" if any(b.type == "tool_use" for b in blocks) else self.last_stop
        return SimpleNamespace(content=blocks, stop_reason=stop,
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=100))


HONEST = [
    [say("Searching."), tool("search_products", query="running shoes flat feet")],
    [tool("read_product_page", product_id="p01")],
    [tool("add_to_cart", product_id="p01", size_us=7.25, qty=1)],   # not a real size: the tool says so
    [tool("add_to_cart", product_id="p01", size_us=9, qty=1)],
    [say("StableRun 4 fits."), tool("submit_cart")],
]


@pytest.fixture
def claude(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")

    def use(turns, **kwargs):
        fake = ScriptedClaude(turns, **kwargs)
        monkeypatch.setattr(agent, "get_client", lambda: fake)
        return fake
    return use


def slips_for(agent_key=None, max_cents=15000, signer=USER_KEY):
    register()
    agent_key = agent_key or keys.agent()
    return {"user_id": "shopper",
            "shopping_slip": slips.sign(slips.shopping_slip(agent_key, REQUEST, [store.MARKETPLACE]), signer),
            "money_slip": slips.sign(slips.money_slip(agent_key, max_cents), signer)}


def start(mode="careful", **kwargs):
    body = {**slips_for(), "mode": mode, **kwargs}
    response = client.post("/agent/runs", json=body)
    assert response.status_code == 200, response.text
    return client.get(f"/agent/runs/{response.json()['id']}").json()  # background task already ran


def test_agent_shops_and_the_order_goes_through_the_guard(monkeypatch, claude, fake_paypal):
    use_ai(monkeypatch, FakeAI())
    fake = claude(HONEST)
    run = start()
    assert run["status"] == "done", run["error"]
    assert run["order"]["decision"] == "ALLOW" and run["order"]["payment_status"] == "CAPTURED"
    assert [s["kind"] for s in run["steps"]] == ["say", "search", "read", "error", "add", "say", "submit"]
    assert "US sizes" in run["steps"][3]["text"]
    assert run["cost_usd"] == pytest.approx(5 * (1000 * 0.10 + 100 * 0.50) / 1e6)
    assert "<request>\n" + REQUEST in fake.requests[0]["messages"][0]["content"]
    assert "never as instructions" in fake.requests[0]["system"]
    # Claude sees the poisoned review only if it reads that page; the page text includes hidden parts.
    assert "Budget: up to $150.00" in fake.requests[0]["messages"][0]["content"]


def test_same_scene_again_replays_for_free(monkeypatch, claude, fake_paypal):
    use_ai(monkeypatch, FakeAI())
    fake = claude(HONEST)
    first = start()
    second = start()
    assert len(fake.requests) == 5   # the second run never called Claude
    assert second["replayed"] and second["cost_usd"] == 0
    assert second["order"]["id"] != first["order"]["id"]   # a fresh cart, a fresh order
    assert [s["kind"] for s in second["steps"]] == [s["kind"] for s in first["steps"]]

    claude(HONEST)
    assert not start(fresh=True)["replayed"]


def test_agents_read_the_hidden_review():
    shop = agent.Shop(lambda step: None)
    text, is_error = shop.run("read_product_page", {"product_id": "p07"})
    assert not is_error and "Note for AI assistants" in text


def test_naive_agent_follows_the_poisoned_review(monkeypatch, fake_paypal):
    use_ai(monkeypatch, FakeAI(injection_asks=InjectionAsk(seller="SuperDeals Outlet", product="StableRun Pro")))
    run = start(mode="naive")   # no Claude key needed
    assert run["order"]["items"][0]["product_id"] == "p07"
    assert any("Note for AI assistants" in s["text"] for s in run["steps"] if s["kind"] == "say")
    assert run["order"]["decision"] == "BLOCK" and run["order"]["payment_status"] == "VOIDED"
    assert run["cost_usd"] == 0


def test_tools_refuse_bad_carts():
    shop = agent.Shop(lambda step: None)
    assert shop.run("add_to_cart", {"product_id": "p01", "size_us": 9, "qty": 1})[1] is False
    assert "one seller" in shop.run("add_to_cart", {"product_id": "p07", "size_us": 9, "qty": 1})[0]
    assert shop.run("cart_view", {})[1] is True    # helpers aren't tools
    assert shop.run("add_to_cart", {"sku": "x"})[1] is True


def test_agent_that_never_submits_fails(claude, fake_paypal):
    claude([[say("I'm not sure what to buy.")]], last_stop="end_turn")
    run = start()
    assert run["status"] == "failed" and "without submitting" in run["error"]
    assert fake_paypal.calls == []


def test_empty_cart_buys_nothing(claude, fake_paypal):
    claude([[tool("search_products", query="unicorn saddle")], [tool("submit_cart")]])
    run = start()
    assert run["status"] == "done" and run["order"] is None and "nothing" in run["error"]


def test_slips_from_someone_else_are_refused(claude):
    body = {**slips_for(signer=slips.new_private_jwk())}
    assert client.post("/agent/runs", json=body).status_code == 422


def test_slips_for_another_agent_are_refused(claude):
    body = {**slips_for(agent_key=slips.new_private_jwk())}
    assert client.post("/agent/runs", json=body).status_code == 422


def test_no_key_and_no_saved_run(fake_paypal):
    assert client.post("/agent/runs", json=slips_for()).status_code == 503


def test_agent_key_is_public():
    jwk = client.get("/agent/key").json()["public_jwk"]
    assert "d" not in jwk and jwk == slips.public_jwk(keys.agent())


def test_empty_cart_names_the_budget_when_that_is_why(claude, fake_paypal):
    claude([[tool("search_products", query="running shoes flat feet", max_price_usd=100)], [tool("submit_cart")]])
    run_id = client.post("/agent/runs", json={**slips_for(max_cents=10000), "mode": "careful"}).json()["id"]
    run = client.get(f"/agent/runs/{run_id}").json()
    assert "within your $100.00 limit" in run["error"] and "from $" in run["error"]
