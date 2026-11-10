"""PayMart, the demo marketplace (design doc, Module 3).

Products come from store_data.json. Each product page is HTML for people; one
review on a SuperDeals listing is hidden with CSS and speaks to AI agents. The
agent view returns all of the page's text, hidden parts included, because
that is what a scraping agent reads.
"""

import html
import json
import secrets
from datetime import date, timedelta
from html.parser import HTMLParser
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import keys, slips

DATA = json.loads((Path(__file__).parent / "store_data.json").read_text())
MARKETPLACE = DATA["marketplace"]
SELLERS = {s["id"]: s for s in DATA["sellers"]}
PRODUCTS = {p["id"]: p for p in DATA["products"]}

router = APIRouter(prefix="/store", tags=["store (demo PayMart)"])


# ---- pages ----

def page_html(product: dict) -> str:
    """The product page as PayMart serves it. Hidden reviews use display:none."""
    seller = SELLERS[product["seller_id"]]
    sizes = ", ".join(f"{s:g}" for s in product["attributes"].get("sizes_us", []))
    reviews = []
    for review in product["reviews"]:
        style = ' style="display:none"' if review.get("hidden") else ""
        reviews.append(
            f'<div class="review"{style}><b>{html.escape(review["author"])}</b> '
            f'{"★" * review["stars"]}<p>{html.escape(review["text"])}</p></div>'
        )
    return (
        f'<article class="product">'
        f'<h1>{html.escape(product["name"])}</h1>'
        f'<p class="seller">Sold by {html.escape(seller["name"])}</p>'
        f'<p class="price">${product["price"] / 100:.2f}</p>'
        f'<p class="description">{html.escape(product["description"])}</p>'
        + (f'<p class="sizes">Sizes (US): {sizes}</p>' if sizes else "")
        + f'<section class="reviews"><h2>Reviews</h2>{"".join(reviews)}</section>'
        f'</article>'
    )


class _AllText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        if data.strip():
            self.parts.append(data.strip())


def page_text(page: str) -> str:
    """Every piece of text on the page, visible or not: what a scraping agent reads."""
    parser = _AllText()
    parser.feed(page)
    return "\n".join(parser.parts)


def product_summary(product: dict) -> dict:
    seller = SELLERS[product["seller_id"]]
    return {
        "id": product["id"],
        "sku": product["sku"],
        "name": product["name"],
        "price": product["price"],
        "seller": {"id": seller["id"], "name": seller["name"]},
        "attributes": product["attributes"],
        "delivery_days": seller["delivery_days"],
    }


def get_product(product_id: str) -> dict:
    product = PRODUCTS.get(product_id)
    if not product:
        raise HTTPException(404, f"No product {product_id!r}")
    return product


# ---- endpoints ----

@router.get("/products")
def list_products(q: str = "", max_price: int | None = None):
    """Search products. max_price is in cents."""
    words = [w for w in q.lower().split() if len(w) > 2]
    found = []
    for product in PRODUCTS.values():
        if max_price is not None and product["price"] > max_price:
            continue
        searchable = " ".join([product["name"], product["description"], json.dumps(product["attributes"])]).lower()
        score = sum(word in searchable for word in words)
        if words and score == 0:
            continue
        found.append((score, product))
    found.sort(key=lambda pair: (-pair[0], pair[1]["price"]))
    return [product_summary(product) for _, product in found]


@router.get("/products/{product_id}")
def show_product(product_id: str, view: str = "human"):
    """view=human: the HTML page. view=agent: all of its text, hidden parts included."""
    product = get_product(product_id)
    page = page_html(product)
    if view == "agent":
        return {"id": product_id, "page_text": page_text(page)}
    return {**product_summary(product), "description": product["description"], "page_html": page}


class CartLine(BaseModel):
    product_id: str
    qty: int = Field(1, ge=1, le=10)
    size_us: float | None = None


class CheckoutRequest(BaseModel):
    items: list[CartLine] = Field(min_length=1, max_length=10)


@router.post("/checkouts")
def create_checkout(request: CheckoutRequest):
    """PayMart prices the cart and signs it (piece 3). The signed cart is valid for 15 minutes."""
    lines, seller_ids = [], set()
    for line in request.items:
        product = get_product(line.product_id)
        sizes = product["attributes"].get("sizes_us")
        if sizes and line.size_us not in sizes:
            raise HTTPException(422, f"{product['name']} isn't available in US size {line.size_us}")
        size = f"-US{line.size_us:g}" if line.size_us is not None else ""
        lines.append({
            "product_id": product["id"],
            "sku": product["sku"] + size,
            "title": product["name"] + (f" (US {line.size_us:g})" if line.size_us is not None else ""),
            "qty": line.qty,
            "unit_price": product["price"],
            "size_us": line.size_us,
        })
        seller_ids.add(product["seller_id"])
    if len(seller_ids) != 1:
        raise HTTPException(422, "A cart must come from one seller")

    seller = SELLERS[seller_ids.pop()]
    claims = slips.cart(
        MARKETPLACE["id"], "chk_" + secrets.token_hex(4), seller["id"], lines,
        delivery_estimate=(date.today() + timedelta(days=seller["delivery_days"])).isoformat(),
    )
    return {"checkout_id": claims["checkout_id"], "cart_jwt": slips.sign(claims, keys.marketplace()), "cart": claims}
