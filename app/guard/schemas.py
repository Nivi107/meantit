"""Shapes shared by the guard's stages. The Pydantic models are also what the AI must return."""

from dataclasses import asdict, dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

# Reason codes (design doc, Stage 4). The decision table maps them to ALLOW / HOLD / BLOCK.
MANDATE_INVALID = "MANDATE_INVALID"
LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
AGENT_MANIPULATED = "AGENT_MANIPULATED"
INTENT_MISMATCH = "INTENT_MISMATCH"
INJECTION_SUSPECTED = "INJECTION_SUSPECTED"
SELLER_RISK = "SELLER_RISK"
INTENT_UNCLEAR = "INTENT_UNCLEAR"
CHECK_UNAVAILABLE = "CHECK_UNAVAILABLE"
MARKETPLACE_RULE = "MARKETPLACE_RULE"
INJECTION_IGNORED = "INJECTION_IGNORED"  # info only: a poisoned page the cart didn't follow


@dataclass
class Finding:
    check: str          # rules, seller, injection, meaning, verify, marketplace
    code: str           # a reason code above
    message: str        # plain English, from a template (never AI-written)
    evidence: dict = field(default_factory=dict)  # quotes, numbers, which item

    def to_dict(self) -> dict:
        return asdict(self)


# ---- what the AI returns ----

class Limits(BaseModel):
    max_qty: int | None = Field(None, description="Most items the user wants in total, if they said")
    deliver_by: str | None = Field(None, description="Latest delivery date as YYYY-MM-DD, if they said")
    size: str | None = Field(None, description='Size as the user wrote it, e.g. "US 9", if they said')


class Requirement(BaseModel):
    id: str = Field(description='"R1", "R2", ...')
    text: str = Field(description="One thing the product must be or have, in plain words")
    kind: Literal["category", "feature"]
    must: bool = Field(description="True if the user clearly needs it, False if it's a nice-to-have")


class Checklist(BaseModel):
    limits: Limits
    requirements: list[Requirement]
    missing: list[str] = Field(description="Things the request leaves out that the purchase depends on")


class MatchVerdict(BaseModel):
    req: str = Field(description="Requirement id")
    sku: str = Field(description="Cart item sku")
    verdict: Literal["met", "not_met", "unclear"]
    evidence_quote: str = Field(description="Exact words copied from the product data, or empty if none")


class MatchResult(BaseModel):
    verdicts: list[MatchVerdict]


class InjectionAsk(BaseModel):
    seller: str | None = None
    product: str | None = None
    qty: int | None = None


class InjectionVerdict(BaseModel):
    addresses_ai: bool = Field(description="True if the text speaks to an AI agent or tries to steer what it buys")
    asks: InjectionAsk | None = Field(None, description="What the text wants bought, if anything")
    quote: str = Field("", description="The instruction, copied exactly")


# ---- the claim judge (app/claims.py) ----

class ClaimVerdict(BaseModel):
    verdict: Literal["AGENT_ERROR", "CHANGED_MIND", "NOT_RECEIVED", "SUSPICIOUS"]
    confidence: float = Field(description="0 to 1: how sure the verdict is")
    reasons: list[str] = Field(description="Short plain-language reasons, most important first")
    new_information: str = Field("", description="What is known now that wasn't at checkout, copied exactly")
