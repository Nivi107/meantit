"""Guard stages 2–5 for an order whose slips already passed verify (stage 1).

Code checks run directly; AI checks run in threads with a timeout. Anything
that fails, times out or is refused becomes CHECK_UNAVAILABLE, so HOLD.
"""

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import date

from app import keys, slips, store
from app.guard import policy
from app.guard.ai import GuardAI
from app.guard.checks import injection, rules, seller
from app.guard.collect import collect
from app.guard.schemas import CHECK_UNAVAILABLE, INTENT_MISMATCH, INTENT_UNCLEAR, Checklist, Finding, MatchResult
from app.guard.verify import VerifyResult

log = logging.getLogger("meantit.guard")

AI_TIMEOUT = 25  # seconds per AI step


@dataclass
class Outcome:
    decision: str
    reason_codes: list[str]
    findings: list[Finding]
    checklist: dict | None = None
    meaning: list[dict] = field(default_factory=list)  # every verdict, met ones included, for the evidence view
    prompt_version: str = ""
    models: dict = field(default_factory=dict)

    @property
    def reasons(self) -> list[str]:
        return [f.message for f in self.findings if policy.DECISION_FOR.get(f.code, policy.HOLD) != policy.ALLOW]


def blocked_by_verify(result: VerifyResult) -> Outcome:
    failure = result.failure
    finding = Finding("verify", failure.code, failure.message, {"check": failure.check})
    return Outcome(policy.BLOCK, [failure.code], [finding])


def run_checks(verified: VerifyResult, ai: GuardAI, today: date | None = None) -> Outcome:
    today = today or date.today()
    cart = verified.cart
    findings: list[Finding] = []

    # Stage 2: collect facts from the marketplace.
    try:
        items = collect(cart)
    except ValueError as err:
        findings.append(Finding("collect", CHECK_UNAVAILABLE, f"Couldn't look up the cart's products: {err}"))
        return _finish(findings, ai)

    # Stage 3: checks. Code checks first, AI checks in parallel threads.
    findings += seller.check(items, today)
    findings += policy.marketplace_rules(store.MARKETPLACE.get("rules", {}), cart)
    segments = [segment for item in items for segment in injection.extract(item.sku, item.page_html)[1]]
    findings += [hit for hit in map(injection.tripwire, segments) if hit]

    checklist, meaning = None, []
    pool = ThreadPoolExecutor(max_workers=8)
    try:
        checklist_job = pool.submit(ai.checklist, verified.intent, today.isoformat())
        classify_jobs = [(segment, pool.submit(ai.classify, segment.text)) for segment in segments]

        checklist = _wait(checklist_job, "meaning", findings)
        match_job = None
        if checklist:
            findings += rules.check(checklist, cart, items)
            match_job = pool.submit(ai.match, checklist.requirements, [_item_for_ai(i) for i in items])

        for segment, job in classify_jobs:
            verdict = _wait(job, "injection", findings)
            if verdict:
                hit = injection.link(segment, verdict, items)
                if hit:
                    findings.append(hit)

        if match_job:
            result = _wait(match_job, "meaning", findings)
            if result:
                meaning, meaning_findings = _meaning(checklist, items, result)
                findings += meaning_findings
    finally:
        pool.shutdown(wait=False, cancel_futures=True)  # don't wait for a timed-out AI call

    return _finish(findings, ai, checklist, meaning)


def _finish(findings, ai, checklist: Checklist | None = None, meaning=None) -> Outcome:
    decision, codes = policy.decide(findings)
    return Outcome(decision, codes, findings, checklist.model_dump() if checklist else None,
                   meaning or [], ai.prompt_version, ai.models)


def _wait(job, check: str, findings: list[Finding]):
    started = time.monotonic()
    try:
        return job.result(timeout=AI_TIMEOUT)
    except FutureTimeout:
        reason = f"timed out after {AI_TIMEOUT}s"
    except Exception as err:  # noqa: BLE001  (any failure means "ask the user")
        reason = str(err) or type(err).__name__
    log.warning("%s check unavailable after %.1fs: %s", check, time.monotonic() - started, reason)
    findings.append(Finding(check, CHECK_UNAVAILABLE, f"The {check} check couldn't finish, so we're asking you.",
                            {"error": reason[:300]}))
    return None


def _item_for_ai(item) -> dict:
    """Catalog facts only. No reviews (that's where injections live), nothing the agent wrote."""
    return {"sku": item.sku, "name": item.product["name"], "description": item.product["description"],
            "attributes": item.product["attributes"]}


def _meaning(checklist: Checklist, items, result: MatchResult) -> tuple[list[dict], list[Finding]]:
    by_key = {(v.req, v.sku): v for v in result.verdicts}
    meaning, findings = [], []
    for req in checklist.requirements:
        for item in items:
            given = by_key.get((req.id, item.sku))
            verdict = given.verdict if given else "unclear"
            quote = given.evidence_quote if given else ""
            # The AI must show proof: a quote that isn't really in the product data counts as unclear.
            if verdict != "unclear" and not _appears_in(quote, item.catalog_text()):
                verdict = "unclear"
            meaning.append({"req": req.id, "text": req.text, "must": req.must, "sku": item.sku,
                            "verdict": verdict, "evidence_quote": quote})
            if not req.must or verdict == "met":
                continue
            evidence = {"sku": item.sku, "requirement": req.text, "quote": quote}
            if verdict == "not_met":
                findings.append(Finding("meaning", INTENT_MISMATCH,
                                        f"{item.product['name']} doesn't fit “{req.text}”.", evidence))
            else:
                findings.append(Finding("meaning", INTENT_UNCLEAR,
                                        f"Not sure {item.product['name']} fits “{req.text}”.", evidence))
    return meaning, findings


def _appears_in(quote: str, text: str) -> bool:
    def norm(s):
        return re.sub(r"\s+", " ", s).strip().lower()
    return bool(norm(quote)) and norm(quote) in norm(text)


def sign_record(order_id: str, checkout_hash: str, bundle: dict, outcome: Outcome) -> str:
    """The Check record: what was decided, why, and hashes of every input. Signed by MeantIt."""
    claims = {
        "iss": "meantit",
        "typ": "meantit.check.1",
        "order_id": order_id,
        "checkout_hash": checkout_hash,
        "decision": outcome.decision,
        "reason_codes": outcome.reason_codes,
        "findings": [f.to_dict() for f in outcome.findings],
        "checklist": outcome.checklist,
        "meaning": outcome.meaning,
        "input_hashes": {kind: slips.hash_of(token) for kind, token in bundle.items()},
        "prompt_version": outcome.prompt_version,
        "models": outcome.models,
        "iat": int(time.time()),
    }
    return slips.sign(claims, keys.check())
