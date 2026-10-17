"""Shared fixtures for the BasketPilot test suite.

The fixtures here build *synthetic* retailers — invented products at invented prices.
That is deliberate and it is the point: the optimizer must be verifiable without a
browser, without an API key, and without a network. Tests that touch a real retailer
live in ``test_browser_live.py`` behind the ``browser`` marker, so that
``pytest -m "not browser"`` stays fast, offline and deterministic.

Every synthetic price is a made-up number. Nothing here is a real observation, and
nothing here may be reported as one.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from basketpilot.models import Product  # noqa: E402
from basketpilot.offers import Item, Offer, RetailerQuote, offer_from_product  # noqa: E402


def make_item(name: str, *, item_id: str | None = None, brand: str | None = None,
              brand_required: bool = False, quantity: float | None = None,
              unit: str | None = None) -> Item:
    return Item(
        item_id=item_id or name.lower().replace(" ", "-"),
        name=name,
        raw=name,
        brand=brand,
        brand_required=brand_required,
        quantity=quantity,
        unit=unit,
    )


def make_offer(
    retailer: str,
    item: Item,
    *,
    name: str,
    price: float | None,
    pack_size: str | None = None,
    brand: str | None = None,
    availability: str = "in_stock",
    match_method: str = "exact_brand_pack",
    confidence: float | None = None,
    substitution: bool = False,
) -> Offer:
    """Build an offer backed by a real, validated :class:`Product`.

    The product goes through ``validate()`` exactly as a scraped one would, so these
    fixtures exercise the same anti-fabrication gate the live path does — including
    its rule that a price must be corroborated by its own evidence.
    """
    displayed = f"₹{price:g}" if price is not None else None
    product = Product(
        query=item.name,
        name=name,
        brand=brand,
        package_size=pack_size,
        price_displayed=displayed,
        # Evidence must contain the price or validate() drops it.
        evidence=f"{name} {pack_size or ''} {displayed or ''}".strip(),
        availability=availability,
    )
    product.validate()

    offer = offer_from_product(
        product,
        retailer_key=retailer,
        retailer=retailer.title(),
        item_id=item.item_id,
        engine="test_fixture",
        source="synthetic",
        match_method=match_method,
        substitution=substitution,
    )
    if confidence is not None:
        offer.match_confidence = confidence
    return offer


def make_quote(retailer: str, **kwargs) -> RetailerQuote:
    return RetailerQuote(retailer_key=retailer, retailer=retailer.title(), **kwargs)


@pytest.fixture
def items() -> list[Item]:
    return [make_item("milk"), make_item("bread"), make_item("eggs")]


@pytest.fixture
def two_retailers(items) -> dict:
    """Two retailers where one covers everything and the other is cheaper on milk.

    This is the canonical shape the optimizer has to get right: the split *looks*
    cheaper on the goods but the extra delivery makes it worse.
    """
    milk, bread, eggs = items
    return {
        "items": items,
        "offers": [
            # alpha: complete, mid-priced
            make_offer("alpha", milk, name="Alpha Toned Milk", price=30.0,
                       pack_size="500 ml", brand="Alpha"),
            make_offer("alpha", bread, name="Alpha Brown Bread", price=45.0,
                       pack_size="400 g", brand="Alpha"),
            make_offer("alpha", eggs, name="Alpha Farm Eggs", price=70.0,
                       pack_size="12 pcs", brand="Alpha"),
            # beta: only milk, and cheaper on it
            make_offer("beta", milk, name="Beta Toned Milk", price=27.0,
                       pack_size="500 ml", brand="Beta"),
        ],
        "quotes": {
            "alpha": make_quote("alpha", delivery_fee=0.0, delivery_fee_verified=True),
            "beta": make_quote("beta", delivery_fee=40.0, delivery_fee_verified=True),
        },
    }
