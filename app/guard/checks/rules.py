"""Rules check (code): quantity, delivery date and size from the checklist."""

import re

from app.guard.schemas import INTENT_MISMATCH, INTENT_UNCLEAR, Checklist, Finding


def check(checklist: Checklist, cart: dict, items) -> list[Finding]:
    findings = []
    limits = checklist.limits

    total_qty = sum(item.qty for item in items)
    if limits.max_qty is not None and total_qty > limits.max_qty:
        findings.append(Finding("rules", INTENT_MISMATCH,
                                f"You asked for {limits.max_qty}, but the cart has {total_qty}.",
                                {"asked": limits.max_qty, "cart": total_qty}))

    if limits.deliver_by:
        estimate = cart.get("delivery_estimate")
        if not estimate:
            findings.append(Finding("rules", INTENT_UNCLEAR,
                                    f"You need it by {limits.deliver_by}, but the seller gives no delivery date."))
        elif estimate > limits.deliver_by:  # both YYYY-MM-DD, so text order is date order
            findings.append(Finding("rules", INTENT_MISMATCH,
                                    f"You need it by {limits.deliver_by}, but it arrives {estimate}.",
                                    {"asked": limits.deliver_by, "cart": estimate}))

    wanted = _size_number(limits.size)
    if limits.size and wanted is None:
        findings.append(Finding("rules", INTENT_UNCLEAR, f"Couldn't read the size {limits.size!r}."))
    for item in items:
        sized = bool(item.product["attributes"].get("sizes_us"))
        if not sized or wanted is None:
            continue
        if item.size_us is None:
            findings.append(Finding("rules", INTENT_UNCLEAR, f"{item.product['name']} has no size in the cart.",
                                    {"sku": item.sku}))
        elif item.size_us != wanted:
            findings.append(Finding("rules", INTENT_MISMATCH,
                                    f"You asked for size US {wanted:g}, but {item.product['name']} is US {item.size_us:g}.",
                                    {"sku": item.sku, "asked": wanted, "cart": item.size_us}))

    for gap in checklist.missing:
        findings.append(Finding("rules", INTENT_UNCLEAR, f"Your request doesn't say: {gap}. The agent had to guess.",
                                {"missing": gap}))
    return findings


def _size_number(size: str | None) -> float | None:
    match = re.search(r"\d+(\.\d+)?", size or "")
    return float(match.group()) if match else None
