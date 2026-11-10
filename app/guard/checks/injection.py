"""Injection check, code layers: find hidden text, trip on obvious instructions, link to the cart.

The AI layer in between (classify) lives in app/guard/ai.py. Hidden text is
only ever data: it is shown to the AI inside data tags, never as instructions.
"""

import re
from dataclasses import dataclass
from html.parser import HTMLParser

from app.guard.schemas import AGENT_MANIPULATED, INJECTION_IGNORED, INJECTION_SUSPECTED, Finding, InjectionVerdict

VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}

_HIDING_STYLES = [
    r"display\s*:\s*none",
    r"visibility\s*:\s*hidden",
    r"font-size\s*:\s*0(\.0+)?(px|em|rem|pt|%)?\s*(;|$)",
    r"opacity\s*:\s*0(\.0+)?\s*(;|$)",
    r"(left|top|text-indent)\s*:\s*-\d{3,}",
    r"(height|width)\s*:\s*0(px)?\s*;.*overflow\s*:\s*hidden",
]
_INVISIBLE_CHARS = re.compile("[​-‏⁠-⁤﻿\U000e0000-\U000e007f]")

TRIPWIRE = re.compile(
    r"\b(ai|a\.i\.)[\s-]+(assistant|agent|model|shopper)s?\b"
    r"|\b(language model|llm|chatbot|shopping agent|system prompt)s?\b"
    r"|\bignore (all |any |the )?(previous|prior|above|earlier)\b"
    r"|\b(always|only|must) (buy|purchase|choose|pick|order|add)\b",
    re.IGNORECASE,
)


@dataclass
class Segment:
    sku: str
    how: str    # why it's hidden: style, comment, attribute, invisible-characters
    text: str


def _hidden_by_style(style: str) -> bool:
    style = style.lower()
    if any(re.search(pattern, style) for pattern in _HIDING_STYLES):
        return True
    color = re.search(r"(?<![-\w])color\s*:\s*([^;]+)", style)
    background = re.search(r"background(-color)?\s*:\s*([^;]+)", style)
    return bool(color and background and color.group(1).strip() == background.group(2).strip())


class _Splitter(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []     # True for each open element that hides its contents
        self.visible, self.hidden = [], []
        self.block = []     # text inside the current outermost hidden element, kept as one segment

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        for name in ("alt", "title", "aria-label"):
            if (attrs.get(name) or "").strip():
                self.hidden.append(("attribute", attrs[name].strip()))
        if tag in VOID_TAGS:
            return
        hides = "hidden" in attrs or _hidden_by_style(attrs.get("style") or "")
        self.stack.append(hides or (bool(self.stack) and self.stack[-1]))

    def handle_endtag(self, tag):
        if tag not in VOID_TAGS and self.stack:
            self.stack.pop()
            if self.block and not (self.stack and self.stack[-1]):
                self.hidden.append(("style", " ".join(self.block)))
                self.block = []

    def close(self):
        super().close()
        if self.block:  # page ended inside a hidden element
            self.hidden.append(("style", " ".join(self.block)))
            self.block = []

    def handle_data(self, data):
        text = data.strip()
        if not text:
            return
        if _INVISIBLE_CHARS.search(text):
            smuggled = "".join(chr(ord(c) - 0xE0000) for c in text if 0xE0000 <= ord(c) <= 0xE007F)
            self.hidden.append(("invisible-characters", smuggled or text))
            text = _INVISIBLE_CHARS.sub("", text).strip()
            if not text:
                return
        if self.stack and self.stack[-1]:
            self.block.append(text)
        else:
            self.visible.append(text)

    def handle_comment(self, data):
        if data.strip():
            self.hidden.append(("comment", data.strip()))


def extract(sku: str, page_html: str) -> tuple[str, list[Segment]]:
    """Split a page into the text a person sees and the hidden segments."""
    splitter = _Splitter()
    splitter.feed(page_html)
    splitter.close()
    hidden = [Segment(sku, how, text) for how, text in splitter.hidden]
    return "\n".join(splitter.visible), hidden


def tripwire(segment: Segment) -> Finding | None:
    """Hidden text that talks to an AI forces at least HOLD, whatever the AI layer says."""
    hit = TRIPWIRE.search(segment.text)
    if not hit:
        return None
    return Finding("injection", INJECTION_SUSPECTED,
                   "A product page hides text aimed at AI agents.",
                   {"sku": segment.sku, "how": segment.how, "quote": segment.text[:300], "matched": hit.group()})


def link(segment: Segment, verdict: InjectionVerdict, items) -> Finding | None:
    """Did the cart do what the hidden text asked?"""
    if not verdict.addresses_ai:
        return None
    asks = verdict.asks
    sellers = [item.seller["name"] for item in items]
    products = [item.product["name"] for item in items]
    followed = bool(asks) and (_mentions(asks.seller, sellers) or _mentions(asks.product, products))
    evidence = {"sku": segment.sku, "how": segment.how, "quote": verdict.quote or segment.text[:300],
                "asks": asks.model_dump() if asks else None}
    if followed:
        return Finding("injection", AGENT_MANIPULATED,
                       "A hidden instruction on the product page told the agent what to buy, and the cart follows it.",
                       evidence)
    return Finding("injection", INJECTION_IGNORED,
                   "A product page hides an instruction for AI agents, but the cart didn't follow it.", evidence)


def _mentions(asked: str | None, names: list[str]) -> bool:
    if not asked:
        return False
    asked = asked.lower().strip()
    return any(asked in name.lower() or name.lower() in asked for name in names)
