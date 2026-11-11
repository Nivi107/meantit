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


def merchant(claim, action, note=""):
    return client.post(f"/claims/{claim['id']}/merchant", json={"action": action, "note": note})


def to_fund(order, text=COVERED):
    """The shopper reports it, PayMart sends it to the Protection Fund, and MeantIt answers in the background."""
    claim = merchant(file(order, text).json(), "send_to_fund").json()
    return client.get(f"/claims/{claim['id']}").json()


# ---- the covered path, end to end ----

def test_agent_error_is_refunded_and_paymart_reimbursed(monkeypatch, money):
    shop, fund = money
    judge = use_judge(monkeypatch, Judge())
    order = paid_order(monkeypatch)

    claim = file(order).json()
    assert claim["status"] == "WITH_PAYMART" and judge.cases == []  # PayMart sees it first; no AI yet
    handed = merchant(claim, "send_to_fund").json()
    assert handed["status"] == "SENT_TO_FUND" and handed["verdict"] is None  # PayMart's click doesn't wait for the judge
    claim = client.get(f"/claims/{claim['id']}").json()  # the judge ran afterwards, on MeantIt's side
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
    claim = to_fund(paid_order(monkeypatch))
    client.post(f"/claims/{claim['id']}/return-received", json={"resellable": False})
    assert fund.payouts == [("PAYMART-MID", 129)]  # only the 1% contribution is in the fund


# ---- PayMart's own decisions ----

def test_paymart_accepts_a_normal_return_without_the_fund(monkeypatch, money):
    shop, fund = money
    judge = use_judge(monkeypatch, Judge())
    order = paid_order(monkeypatch)
    claim = merchant(file(order, "I found the same shoes cheaper elsewhere.").json(), "accept_return").json()
    assert claim["status"] == "STORE_RETURN"
    done = client.post(f"/claims/{claim['id']}/return-received").json()
    assert done["status"] == "STORE_REFUNDED" and ("refund", "PP-CAPTURE") in shop.calls
    assert fund.payouts == [] and judge.cases == []  # PayMart's own costs; no fund, no AI
    assert client.get(f"/orders/{order['id']}").json()["payment_status"] == "REFUNDED"


def test_paymart_declines(monkeypatch, money):
    use_judge(monkeypatch, Judge())
    claim = merchant(file(paid_order(monkeypatch)).json(), "decline", note="Worn outdoors").json()
    assert claim["status"] == "STORE_DECLINED" and "Worn outdoors" in claim["status_reason"]
    assert client.post(f"/claims/{claim['id']}/return-received").status_code == 409


def test_paymart_can_still_take_it_back_after_the_fund_says_no(monkeypatch, money):
    use_judge(monkeypatch, Judge("CHANGED_MIND", 0.95))
    claim = to_fund(paid_order(monkeypatch), "I found the same shoe cheaper somewhere else.")
    assert merchant(claim, "send_to_fund").status_code == 409  # the fund decides once
    assert merchant(claim, "accept_return").json()["status"] == "STORE_RETURN"


# ---- the judge's verdict, applied by code ----

def test_changed_mind_is_declined(monkeypatch, money):
    use_judge(monkeypatch, Judge("CHANGED_MIND", 0.95))
    claim = to_fund(paid_order(monkeypatch), "I found the same shoe cheaper somewhere else.")
    assert claim["status"] == "DECLINED"
    assert client.post(f"/claims/{claim['id']}/return-received").status_code == 409


def test_not_received_goes_to_paypal_not_the_fund(monkeypatch, money):
    use_judge(monkeypatch, Judge("NOT_RECEIVED", 0.9))
    claim = to_fund(paid_order(monkeypatch), "The parcel never arrived at my house.")
    assert claim["status"] == "NOT_COVERED" and "Seller Protection" in claim["status_reason"]


@pytest.mark.parametrize("verdict, confidence", [("AGENT_ERROR", 0.5), ("SUSPICIOUS", 0.9)])
def test_unsure_or_suspicious_goes_to_a_person(monkeypatch, money, verdict, confidence):
    use_judge(monkeypatch, Judge(verdict, confidence))
    claim = to_fund(paid_order(monkeypatch))
    assert claim["status"] == "IN_REVIEW"
    reviewed = client.post(f"/claims/{claim['id']}/review", json={"approve": True, "note": "checked photos"}).json()
    assert reviewed["status"] == "APPROVED" and reviewed["reviewed_by"] == "admin"
    assert "checked photos" in reviewed["status_reason"]


def test_no_judge_means_a_person_decides(monkeypatch, money):
    use_judge(monkeypatch, claims.guard_ai.NoAI())
    claim = to_fund(paid_order(monkeypatch))
    assert claim["status"] == "IN_REVIEW" and claim["verdict"] is None


# ---- coverage rules, checked before any AI ----

def test_approving_a_hold_yourself_is_not_covered(monkeypatch, money):
    judge = use_judge(monkeypatch, Judge())
    use_ai(monkeypatch, FakeAI(verdict="not_met", quote="Flat cushioned sole, lifestyle design"))
    order = place_order("p11")[1].json()
    client.post(f"/orders/{order['id']}/approve")
    claim = to_fund(order)
    assert claim["status"] == "NOT_COVERED" and "approved it anyway" in claim["status_reason"]
    assert judge.cases == []  # code stopped it; the AI was never asked


def test_repeat_claimer_goes_to_review(monkeypatch, money):
    judge = use_judge(monkeypatch, Judge())
    for _ in range(claims.REPEAT_CLAIMS):
        to_fund(paid_order(monkeypatch))
    claim = to_fund(paid_order(monkeypatch))
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


def test_a_person_can_decide_a_claim_the_judge_never_answered(monkeypatch, money):
    claim = file(paid_order(monkeypatch)).json()
    monkeypatch.setattr(claims, "_fund_decides", lambda claim_id: None)  # the server restarted mid-judge
    assert merchant(claim, "send_to_fund").json()["status"] == "SENT_TO_FUND"
    done = client.post(f"/claims/{claim['id']}/review", json={"approve": True}).json()
    assert done["status"] == "APPROVED" and done["reviewed_by"] == "admin"


def test_a_late_judge_doesnt_overwrite_a_person(monkeypatch, money):
    judge = use_judge(monkeypatch, Judge())
    claim = file(paid_order(monkeypatch)).json()
    fund_decides = claims._fund_decides
    monkeypatch.setattr(claims, "_fund_decides", lambda claim_id: None)
    merchant(claim, "send_to_fund")
    client.post(f"/claims/{claim['id']}/review", json={"approve": False})
    fund_decides(claim["id"])  # the judge finally answers, approving
    assert judge.cases  # it did run
    assert client.get(f"/claims/{claim['id']}").json()["status"] == "DECLINED"
