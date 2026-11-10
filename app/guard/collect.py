"""Guard stage 2: gather the facts the checks need, straight from the marketplace.

Never from the agent: not its summaries, not its reasoning, not pages it says it read.
"""

import json
from dataclasses import dataclass
from statistics import median

from app import store


@dataclass
class ItemFacts:
    sku: str                 # as in the cart, e.g. "RS-204-US9"
    product: dict            # catalog entry
    seller: dict
    qty: int
    size_us: float | None
    page_html: str
    category_median_price: int | None

    def catalog_text(self) -> str:
        """The product data the meaning check may quote: name, description, attributes. No reviews."""
        attrs = json.dumps(self.product["attributes"], ensure_ascii=False)
        return f'{self.product["name"]}\n{self.product["description"]}\n{attrs}'


def collect(cart: dict) -> list[ItemFacts]:
    facts = []
    for line in cart["items"]:
        product = store.PRODUCTS.get(line.get("product_id"))
        if not product:
            raise ValueError(f"cart item {line.get('sku')!r} isn't in the catalog")
        category = product["attributes"].get("category")
        prices = [p["price"] for p in store.PRODUCTS.values() if p["attributes"].get("category") == category]
        facts.append(ItemFacts(
            sku=line["sku"],
            product=product,
            seller=store.SELLERS[product["seller_id"]],
            qty=line["qty"],
            size_us=line.get("size_us"),
            page_html=store.page_html(product),
            category_median_price=int(median(prices)) if prices else None,
        ))
    return facts
