"""Measure the guard's AI checks against hand-labelled cases. Real Claude calls: about 39, ~$0.10.

    .venv/bin/python scripts/ai_eval.py            # everything
    .venv/bin/python scripts/ai_eval.py classify   # just one part (classify is the cheapest, Haiku)

Cases are in scripts/ai_eval_cases.json. Run it after changing a prompt and compare the scores.
"""

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config, store  # noqa: E402
from app.guard import ai  # noqa: E402
from app.guard.collect import ItemFacts  # noqa: E402
from app.guard.pipeline import _appears_in, _item_for_ai  # noqa: E402
from app.guard.schemas import Requirement  # noqa: E402

PRICES = {"claude-sonnet-5-5": (2.00, 10.00), "claude-haiku-5-5": (0.10, 0.50)}
CASES = json.loads((Path(__file__).parent / "ai_eval_cases.json").read_text())

if not config.ANTHROPIC_API_KEY:
    sys.exit("Set ANTHROPIC_API_KEY in .env.")
logging.getLogger("meantit.ai").setLevel(logging.WARNING)

spent = []
claude = ai.default_ai()
original = claude.client.messages.parse


def counted(**kwargs):
    response = original(**kwargs)
    price_in, price_out = PRICES[kwargs["model"]]
    spent.append((response.usage.input_tokens * price_in + response.usage.output_tokens * price_out) / 1e6)
    return response


claude.client.messages.parse = counted


def run(label, cases, check):
    passed = 0
    for case in cases:
        try:
            problems = check(case)
        except ai.AIUnavailable as err:
            problems = [f"unavailable: {err}"]
        passed += not problems
        if problems:
            print(f"  ✗ {json.dumps(case)[:110]}\n      " + "; ".join(problems))
    print(f"{label}: {passed}/{len(cases)}")
    return passed, len(cases)


def check_classify(case):
    got = claude.classify(case["text"])
    problems = []
    if got.addresses_ai != case["addresses_ai"]:
        problems.append(f"addresses_ai={got.addresses_ai}")
    for field in ("seller", "product"):
        want = case.get(field)
        have = getattr(got.asks, field, None) if got.asks else None
        if want and want.lower() not in (have or "").lower():
            problems.append(f"asks.{field}={have!r}")
    return problems


def check_checklist(case):
    got = claude.checklist(case["request"], case["today"])
    problems = []
    for field in ("max_qty", "deliver_by"):
        if field in case and getattr(got.limits, field) != case[field]:
            problems.append(f"{field}={getattr(got.limits, field)!r}")
    if "size_has" in case and case["size_has"] not in (got.limits.size or ""):
        problems.append(f"size={got.limits.size!r}")
    texts = " | ".join(r.text.lower() for r in got.requirements)
    problems += [f"no requirement about {word!r} in {texts!r}" for word in case["requirements"] if word not in texts]
    if bool(got.missing) != case["missing"]:
        problems.append(f"missing={got.missing}")
    return problems


def check_match(case):
    product = store.PRODUCTS[case["product"]]
    item = ItemFacts(sku=product["sku"], product=product, seller={}, qty=1, size_us=9, page_html="",
                     category_median_price=0)
    got = claude.match([Requirement(id="R1", text=case["req"], kind="feature", must=True)], [_item_for_ai(item)])
    verdict = got.verdicts[0] if got.verdicts else None
    if not verdict:
        return ["no verdict"]
    problems = [] if verdict.verdict == case["verdict"] else [f"verdict={verdict.verdict} ({verdict.evidence_quote!r})"]
    if verdict.verdict != "unclear" and not _appears_in(verdict.evidence_quote, item.catalog_text()):
        problems.append(f"quote not in product data: {verdict.evidence_quote!r}")
    return problems


parts = {"classify": check_classify, "checklist": check_checklist, "match": check_match}
chosen = sys.argv[1:] or list(parts)
totals = [run(name, CASES[name], parts[name]) for name in chosen]
print(f"\nTotal {sum(p for p, _ in totals)}/{sum(n for _, n in totals)}, "
      f"{len(spent)} Claude calls, about ${sum(spent):.4f}")
