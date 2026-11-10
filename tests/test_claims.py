"""Claims and the Protection Fund, with a fake PayPal and a fake judge (no network, no cost)."""

import pytest
from fastapi.testclient import TestClient

from app import claims, paypal
from app.guard.schemas import ClaimVerdict
from app.main import app
from test_guard import FakeAI, FakePayPal, place_order, use_ai

client = TestClient(app)
COVERED = "I have flat feet and these have no support at all, the midsole is soft and flat. Not what I asked for."


class FakePayPalWithRefunds(FakePayPal):
    def refund_capture(self, capture_id, amount_cents=None, **kwargs):
        self.calls.append(("refund", capture_id))
        return {"id": "PP-REFUND", "status": "COMPLETED"}

    def get_capture(self, capture_id):
        return {"id": capture_id, "payee": {"merchant_id": "PAYMART-MID"},
                "seller_receivable_breakdown": {"paypal_fee": {"currency_code": "USD", "value": "4.99"}}}


class FakeFund:
    def __init__(self):
        self.payouts = []

    def create_payout(self, receiver, amount_cents, **kwargs):
        self.payouts.append((receiver, amount_cents))
        return {"batch_header": {"payout_batch_id": "PP-BATCH", "batch_status": "PENDING"}}

    def get_payout(self, batch_id):
        return {"batch_header": {"payout_batch_id": batch_id, "batch_status": "SUCCESS"}}


class Judge:
    def __init__(self, verdict="AGENT_ERROR", confidence=0.9):
        self.answer = ClaimVerdict(verdict=verdict, confidence=confidence, reasons=["because"])
        self.cases = []
        self.models = {"judge": "fake-judge"}

    def judge(self, case):
        self.cases.append(case)
        return self.answer


@pytest.fixture
def money(monkeypatch):
    shop, fund = FakePayPalWithRefunds(), FakeFund()
    monkeypatch.setattr(paypal, "paymart", lambda: shop)
    monkeypatch.setattr(paypal, "fund", lambda: fund)
    monkeypatch.setattr(paypal.config, "SANDBOX_VAULT_ID", "VAULT-1")
    return shop, fund


def use_judge(monkeypatch, judge):
    monkeypatch.setattr(claims, "get_ai", lambda: judge)
    return judge


def paid_order(monkeypatch, product="p01"):
    use_ai(monkeypatch, FakeAI())
    order = place_order(product)[1].json()
    assert order["payment_status"] == "CAPTURED"
    return order


def file(order, text=COVERED, user="shopper"):
    return client.post(f"/orders/{order['id']}/claims", json={"text": text}, headers={"X-User-Id": user})


# ---- the covered path, end to end ----

def test_agent_error_is_refunded_and_paymart_reimbursed(monkeypatch, money):
    shop, fund = money
    judge = use_judge(monkeypatch, Judge())
    order = paid_order(monkeypatch)

    claim = file(order).json()
    assert claim["status"] == "APPROVED" and claim["reviewed_by"] == "judge"
    case = judge.cases[0]
    assert case["signed_request"] == order["intent"] and case["shopper_claim"] == COVERED
    assert "no medial post" in case["published_after_checkout"][0]["text"]  # news the guard couldn't have had

    done = client.post(f"/claims/{claim['id']}/return-received", json={"resellable": True}).json()
    assert done["status"] == "REIMBURSED"
    assert ("refund", "PP-CAPTURE") in shop.calls
    assert done["losses"] == {"return_shipping": 800, "paypal_fee": 499, "unsellable_item": 0}
    assert fund.payouts == [("PAYMART-MID", 1299)]
    assert client.get(f"/orders/{order['id']}").json()["payment_status"] == "REFUNDED"

    ledger = client.get("/fund").json()
    assert ledger["balance"] == claims.STARTING_RESERVE + 129 - 1299  # reserve + 1% of $129 - reimbursement
    assert client.get(f"/claims/{claim['id']}").json()["payout_status"] == "SUCCESS"
    assert client.post(f"/claims/{claim['id']}/return-received").status_code == 409  # never pays twice


def test_reimbursement_never_exceeds_the_fund(monkeypatch, money):
    _, fund = money
    use_judge(monkeypatch, Judge())
    monkeypatch.setattr(claims, "STARTING_RESERVE", 0)
    claim = file(paid_order(monkeypatch)).json()
    client.post(f"/claims/{claim['id']}/return-received", json={"resellable": False})
    assert fund.payouts == [("PAYMART-MID", 129)]  # only the 1% contribution is in the fund


# ---- the judge's verdict, applied by code ----

def test_changed_mind_is_declined(monkeypatch, money):
    use_judge(monkeypatch, Judge("CHANGED_MIND", 0.95))
    claim = file(paid_order(monkeypatch), "I found the same shoe cheaper somewhere else.").json()
    assert claim["status"] == "DECLINED"
    assert client.post(f"/claims/{claim['id']}/return-received").status_code == 409


def test_not_received_goes_to_paypal_not_the_fund(monkeypatch, money):
    use_judge(monkeypatch, Judge("NOT_RECEIVED", 0.9))
    claim = file(paid_order(monkeypatch), "The parcel never arrived at my house.").json()
    assert claim["status"] == "NOT_COVERED" and "Seller Protection" in claim["status_reason"]


@pytest.mark.parametrize("verdict, confidence", [("AGENT_ERROR", 0.5), ("SUSPICIOUS", 0.9)])
def test_unsure_or_suspicious_goes_to_a_person(monkeypatch, money, verdict, confidence):
    use_judge(monkeypatch, Judge(verdict, confidence))
    claim = file(paid_order(monkeypatch)).json()
    assert claim["status"] == "IN_REVIEW"
    reviewed = client.post(f"/claims/{claim['id']}/review", json={"approve": True, "note": "checked photos"}).json()
    assert reviewed["status"] == "APPROVED" and reviewed["reviewed_by"] == "admin"
    assert "checked photos" in reviewed["status_reason"]


def test_no_judge_means_a_person_decides(monkeypatch, money):
    use_judge(monkeypatch, claims.guard_ai.NoAI())
    claim = file(paid_order(monkeypatch)).json()
    assert claim["status"] == "IN_REVIEW" and claim["verdict"] is None


# ---- coverage rules, checked before any AI ----

def test_approving_a_hold_yourself_is_not_covered(monkeypatch, money):
    judge = use_judge(monkeypatch, Judge())
    use_ai(monkeypatch, FakeAI(verdict="not_met", quote="Flat cushioned sole, lifestyle design"))
    order = place_order("p11")[1].json()
    client.post(f"/orders/{order['id']}/approve")
    claim = file(order).json()
    assert claim["status"] == "NOT_COVERED" and "approved it anyway" in claim["status_reason"]
    assert judge.cases == []  # code stopped it; the AI was never asked


def test_repeat_claimer_goes_to_review(monkeypatch, money):
    judge = use_judge(monkeypatch, Judge())
    for _ in range(claims.REPEAT_CLAIMS):
        file(paid_order(monkeypatch))
    claim = file(paid_order(monkeypatch)).json()
    assert claim["status"] == "IN_REVIEW" and "other claims" in claim["status_reason"]
    assert len(judge.cases) == claims.REPEAT_CLAIMS


def test_claim_rules(monkeypatch, money):
    use_judge(monkeypatch, Judge())
    order = paid_order(monkeypatch)
    assert file(order, user="someone-else").status_code == 403
    assert file(order).status_code == 200
    assert file(order).status_code == 409  # one claim per order
    use_ai(monkeypatch, FakeAI(verdict="not_met", quote="Flat cushioned sole, lifestyle design"))
    held = place_order("p11")[1].json()
    assert file(held).status_code == 409  # nothing was charged


def test_claims_list_for_one_shopper(monkeypatch, money):
    use_judge(monkeypatch, Judge())
    claim = file(paid_order(monkeypatch)).json()
    assert [c["id"] for c in client.get("/claims", params={"user_id": "shopper"}).json()] == [claim["id"]]
    assert client.get("/claims", params={"user_id": "nobody"}).json() == []
