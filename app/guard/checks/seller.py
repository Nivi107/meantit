"""Seller check (code): known seller, how new, normal price."""

from datetime import date

from app.guard.schemas import SELLER_RISK, Finding

NEW_SELLER_DAYS = 30
PRICE_OVER_MEDIAN = 1.30


def check(items, today: date) -> list[Finding]:
    findings = []
    for seller in {item.seller["id"]: item.seller for item in items}.values():
        if not seller["registered"]:
            findings.append(Finding("seller", SELLER_RISK, f"{seller['name']} is not a registered seller.",
                                    {"seller": seller["name"]}))
        age = (today - date.fromisoformat(seller["joined_at"])).days
        if age < NEW_SELLER_DAYS:
            findings.append(Finding("seller", SELLER_RISK, f"{seller['name']} joined {age} days ago.",
                                    {"seller": seller["name"], "days": age}))
    for item in items:
        median = item.category_median_price
        if median and item.product["price"] > median * PRICE_OVER_MEDIAN:
            over = round(100 * (item.product["price"] / median - 1))
            findings.append(Finding("seller", SELLER_RISK,
                                    f"{item.product['name']} costs {over}% more than similar items.",
                                    {"sku": item.sku, "price": item.product["price"], "median": median}))
    return findings
