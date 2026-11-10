"""Guard stage 4: the decision table. Code decides, never the AI."""

from app.guard.schemas import (AGENT_MANIPULATED, CHECK_UNAVAILABLE, INJECTION_IGNORED, INJECTION_SUSPECTED,
                               INTENT_MISMATCH, INTENT_UNCLEAR, LIMIT_EXCEEDED, MANDATE_INVALID, MARKETPLACE_RULE,
                               SELLER_RISK, Finding)

ALLOW, HOLD, BLOCK = "ALLOW", "HOLD", "BLOCK"

DECISION_FOR = {
    MANDATE_INVALID: BLOCK,
    LIMIT_EXCEEDED: BLOCK,
    AGENT_MANIPULATED: BLOCK,
    INTENT_MISMATCH: HOLD,
    INJECTION_SUSPECTED: HOLD,
    SELLER_RISK: HOLD,
    INTENT_UNCLEAR: HOLD,
    CHECK_UNAVAILABLE: HOLD,
    MARKETPLACE_RULE: HOLD,
    INJECTION_IGNORED: ALLOW,  # reported to the marketplace, doesn't stop the order
}
_RANK = {ALLOW: 0, HOLD: 1, BLOCK: 2}


def decide(findings: list[Finding]) -> tuple[str, list[str]]:
    """The strictest decision any finding calls for, and the reason codes behind it.

    A code missing from the table means HOLD: when unsure, ask the user.
    """
    decision = ALLOW
    for finding in findings:
        wanted = DECISION_FOR.get(finding.code, HOLD)
        if _RANK[wanted] > _RANK[decision]:
            decision = wanted
    codes = sorted({f.code for f in findings if DECISION_FOR.get(f.code, HOLD) != ALLOW})
    return decision, codes


def marketplace_rules(rules: dict, cart: dict) -> list[Finding]:
    """Extra rows a marketplace adds, e.g. "hold agent orders over $200"."""
    findings = []
    limit = rules.get("hold_over")
    if limit is not None and cart["total"] > limit:
        findings.append(Finding("marketplace", MARKETPLACE_RULE,
                                f"PayMart holds agent orders over ${limit / 100:.2f}.",
                                {"limit": limit, "total": cart["total"]}))
    return findings
