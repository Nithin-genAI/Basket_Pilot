"""Phase 2 schema: shopping-list items, retailer offers, and per-retailer quotes.

Phase 1's :class:`~basketpilot.models.Product` is reused unchanged — it carries the
anti-fabrication gate and everything downstream depends on it. This module adds the
three things a *basket* needs that a product search does not:

* :class:`Item` — one line of the user's list, with the quantity and brand
  preference teased out of it.
* :class:`Offer` — a product, tied to the item it satisfies, with the match
  confidence and unit price the optimizer compares on.
* :class:`RetailerQuote` — what a retailer charges on top of the goods.

The governing rule from the brief is *"do not treat missing values as zero"*, and it
bites hardest on :class:`RetailerQuote`. A delivery fee we did not see is not a
delivery fee of ₹0; it is an unknown. So fees are ``float | None`` paired with an
explicit ``*_verified`` flag, and :meth:`RetailerQuote.unverified_charges` names
every unknown so a total can be reported as an estimate rather than a fact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .models import Product

# --------------------------------------------------------------------------- #
# Units
# --------------------------------------------------------------------------- #

# Everything is normalised to one of three base units so unit prices are
# comparable across pack sizes: grams, millilitres, or a plain count.
UNIT_ALIASES: dict[str, tuple[str, float]] = {
    "g": ("g", 1.0),
    "gm": ("g", 1.0),
    "gms": ("g", 1.0),
    "gram": ("g", 1.0),
    "grams": ("g", 1.0),
    "kg": ("g", 1000.0),
    "kgs": ("g", 1000.0),
    "kilo": ("g", 1000.0),
    "kilos": ("g", 1000.0),
    "kilogram": ("g", 1000.0),
    "ml": ("ml", 1.0),
    "millilitre": ("ml", 1.0),
    "millilitres": ("ml", 1.0),
    "l": ("ml", 1000.0),
    "lt": ("ml", 1000.0),
    "ltr": ("ml", 1000.0),
    "litre": ("ml", 1000.0),
    "litres": ("ml", 1000.0),
    "liter": ("ml", 1000.0),
    "liters": ("ml", 1000.0),
    "pc": ("count", 1.0),
    "pcs": ("count", 1.0),
    "piece": ("count", 1.0),
    "pieces": ("count", 1.0),
    "pack": ("count", 1.0),
    "packs": ("count", 1.0),
    "dozen": ("count", 12.0),
    "dozens": ("count", 12.0),
    "unit": ("count", 1.0),
    "units": ("count", 1.0),
}

# `500 g`, `1.5L`, `2 ltr`, `1 kg`, `6 x 100 g`
_SIZE_RE = re.compile(
    r"(?P<qty>\d+(?:\.\d+)?)\s*(?P<unit>[a-zA-Z]+)",
)
# A multipack: "6 x 100 g" — the outer count multiplies the inner size.
_MULTIPACK_RE = re.compile(
    r"(?P<count>\d+)\s*[x×]\s*(?P<qty>\d+(?:\.\d+)?)\s*(?P<unit>[a-zA-Z]+)",
)


def parse_pack_size(text: str | None) -> tuple[float | None, str | None]:
    """Parse a package-size string into ``(quantity, base_unit)``.

    ``"500 ml"`` -> ``(500.0, "ml")``; ``"1 kg"`` -> ``(1000.0, "g")``;
    ``"6 x 100 g"`` -> ``(600.0, "g")``.

    Returns ``(None, None)`` when nothing recognisable is present. It never guesses:
    a pack size that cannot be read stays unread, because a wrong unit price is worse
    than a missing one — the optimizer would silently prefer the wrong pack.
    """
    if not text:
        return None, None
    lowered = text.lower().strip()

    multipack = _MULTIPACK_RE.search(lowered)
    if multipack:
        unit = UNIT_ALIASES.get(multipack.group("unit"))
        if unit:
            base_unit, factor = unit
            total = (
                float(multipack.group("count"))
                * float(multipack.group("qty"))
                * factor
            )
            return total, base_unit

    # Take the first size-looking token. "Amul Taaza 1 L" -> 1 L; the brand words
    # before it are not sizes and are skipped because they have no digit.
    for match in _SIZE_RE.finditer(lowered):
        unit = UNIT_ALIASES.get(match.group("unit"))
        if unit:
            base_unit, factor = unit
            return float(match.group("qty")) * factor, base_unit
    return None, None


def unit_price(price: float | None, quantity: float | None, unit: str | None) -> float | None:
    """Price per 100 g / per litre / per item — the comparable figure.

    Returns ``None`` when any input is missing. Deliberately *not* zero: a product
    with no readable pack size has no comparable unit price, and pretending it does
    would let it win a comparison it never entered.
    """
    if price is None or quantity is None or not unit or quantity <= 0:
        return None
    if unit == "g":
        return round(price / quantity * 100, 4)
    if unit == "ml":
        return round(price / quantity * 1000, 4)
    return round(price / quantity, 4)


def unit_label(unit: str | None) -> str:
    return {"g": "per 100 g", "ml": "per litre", "count": "per item"}.get(unit or "", "")


# --------------------------------------------------------------------------- #
# Items
# --------------------------------------------------------------------------- #


@dataclass
class Item:
    """One line of the user's shopping list, normalised."""

    item_id: str
    name: str
    raw: str
    quantity: float | None = None
    unit: str | None = None
    brand: str | None = None
    # True when the user named a brand, which makes it a constraint rather than a
    # hint: the optimizer must not substitute a different brand for it.
    brand_required: bool = False
    parsed_by: str = "deterministic"

    @property
    def search_query(self) -> str:
        """The string to hand a retailer. Brand first, because that is what narrows."""
        parts = [p for p in (self.brand, self.name) if p]
        return " ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "name": self.name,
            "raw": self.raw,
            "quantity": self.quantity,
            "unit": self.unit,
            "brand": self.brand,
            "brand_required": self.brand_required,
            "parsed_by": self.parsed_by,
        }


# --------------------------------------------------------------------------- #
# Offers
# --------------------------------------------------------------------------- #

# How a product came to be matched to an item. Ordered strongest first; the
# optimizer and the explanation both read this, so it is a closed vocabulary rather
# than a free-text note.
MATCH_EXACT = "exact_brand_pack"
MATCH_BRAND = "brand_match"
MATCH_NAME = "name_match"
MATCH_FUZZY = "fuzzy_name"
MATCH_LLM = "llm_resolved"

MATCH_CONFIDENCE = {
    MATCH_EXACT: 0.98,
    MATCH_BRAND: 0.85,
    MATCH_NAME: 0.75,
    MATCH_LLM: 0.70,
    MATCH_FUZZY: 0.55,
}


@dataclass
class Offer:
    """A real product from a real retailer, tied to the item it satisfies."""

    retailer_key: str
    retailer: str
    item_id: str
    product: Product
    match_method: str = MATCH_FUZZY
    match_confidence: float = 0.0
    pack_quantity: float | None = None
    pack_unit: str | None = None
    unit_price: float | None = None
    engine: str = ""
    source: str = ""
    captured_at: str = ""
    # Set when the product is a substitution the user has not approved. The optimizer
    # surfaces these and refuses to treat the basket as final.
    substitution: bool = False
    # Set when the pack size differs from what the item asked for — a 250 ml carton
    # against a 2 L request. The product is real and relevant; it is just not the size
    # that was asked for, so it needs approval rather than silent inclusion.
    pack_mismatch: bool = False
    pack_note: str = ""

    @property
    def price(self) -> float | None:
        return self.product.price_numeric

    @property
    def in_stock(self) -> bool:
        return self.product.availability not in ("out_of_stock", "unavailable")

    @property
    def product_url(self) -> str | None:
        return self.product.product_url

    def to_dict(self) -> dict[str, Any]:
        return {
            "retailer_key": self.retailer_key,
            "retailer": self.retailer,
            "item_id": self.item_id,
            "name": self.product.name,
            "brand": self.product.brand,
            "pack_size": self.product.package_size,
            "pack_quantity": self.pack_quantity,
            "pack_unit": self.pack_unit,
            "price": self.price,
            "price_displayed": self.product.price_displayed,
            "unit_price": self.unit_price,
            "unit_price_label": unit_label(self.pack_unit),
            "in_stock": self.in_stock,
            "availability": self.product.availability,
            "product_url": self.product_url,
            "match_method": self.match_method,
            "match_confidence": self.match_confidence,
            "substitution": self.substitution,
            "pack_mismatch": self.pack_mismatch,
            "pack_note": self.pack_note,
            "engine": self.engine,
            "source": self.source,
            "captured_at": self.captured_at,
            "warnings": list(self.product.warnings),
        }


def offer_from_product(
    product: Product,
    *,
    retailer_key: str,
    retailer: str,
    item_id: str,
    engine: str,
    source: str,
    match_method: str = MATCH_FUZZY,
    substitution: bool = False,
) -> Offer:
    """Build an offer from a validated product, deriving pack size and unit price."""
    quantity, unit = parse_pack_size(product.package_size)
    return Offer(
        retailer_key=retailer_key,
        retailer=retailer,
        item_id=item_id,
        product=product,
        match_method=match_method,
        match_confidence=MATCH_CONFIDENCE.get(match_method, 0.5),
        pack_quantity=quantity,
        pack_unit=unit,
        unit_price=unit_price(product.price_numeric, quantity, unit),
        engine=engine,
        source=source,
        captured_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        substitution=substitution,
    )


# --------------------------------------------------------------------------- #
# Retailer quotes — fees, discounts, delivery
# --------------------------------------------------------------------------- #


@dataclass
class RetailerQuote:
    """What one retailer charges beyond the goods themselves.

    Every money field is ``None`` when unobserved, paired with a flag saying whether
    we actually verified it. ``None`` is not zero: a total built on an unverified fee
    is an estimate, and the optimizer says so instead of quietly ranking it as cheap.
    """

    retailer_key: str
    retailer: str
    delivery_fee: float | None = None
    delivery_fee_verified: bool = False
    free_delivery_threshold: float | None = None
    handling_fee: float | None = None
    handling_fee_verified: bool = False
    small_cart_fee: float | None = None
    small_cart_fee_verified: bool = False
    discount: float | None = None
    discount_verified: bool = False
    discount_note: str | None = None
    min_order: float | None = None
    delivery_eta: str | None = None
    notes: list[str] = field(default_factory=list)

    def unverified_charges(self) -> list[str]:
        """Name every charge we know may exist but could not confirm.

        This is the list that turns a confident total into an honest estimate. It is
        deliberately explicit about *what* is unknown, because "costs may vary" is not
        actionable and "delivery fee not verified" is.
        """
        unknown: list[str] = []
        if not self.delivery_fee_verified:
            unknown.append(
                "delivery fee not verified"
                + (f" (free above ₹{self.free_delivery_threshold:g})" if self.free_delivery_threshold else "")
            )
        if not self.handling_fee_verified:
            unknown.append("handling fee not verified")
        if not self.small_cart_fee_verified:
            unknown.append("small-cart fee not verified")
        if not self.discount_verified:
            unknown.append("discounts not verified")
        return unknown

    def verified_fees(self, subtotal: float) -> float:
        """The fee total we can actually stand behind. Unknown fees contribute nothing.

        Note this makes the *reported* total a floor, not a guess: it is the amount we
        can prove. The unknowns are listed alongside so the number is never read as
        final.
        """
        total = 0.0
        if self.delivery_fee_verified and self.delivery_fee is not None:
            if self.free_delivery_threshold is not None and subtotal >= self.free_delivery_threshold:
                pass  # delivery is free above the threshold
            else:
                total += self.delivery_fee
        if self.handling_fee_verified and self.handling_fee is not None:
            total += self.handling_fee
        if (
            self.small_cart_fee_verified
            and self.small_cart_fee is not None
            and self.min_order is not None
            and subtotal < self.min_order
        ):
            total += self.small_cart_fee
        return round(total, 2)

    def verified_discount(self) -> float:
        if self.discount_verified and self.discount is not None:
            return round(self.discount, 2)
        return 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "retailer_key": self.retailer_key,
            "retailer": self.retailer,
            "delivery_fee": self.delivery_fee,
            "delivery_fee_verified": self.delivery_fee_verified,
            "free_delivery_threshold": self.free_delivery_threshold,
            "handling_fee": self.handling_fee,
            "handling_fee_verified": self.handling_fee_verified,
            "small_cart_fee": self.small_cart_fee,
            "small_cart_fee_verified": self.small_cart_fee_verified,
            "discount": self.discount,
            "discount_verified": self.discount_verified,
            "discount_note": self.discount_note,
            "min_order": self.min_order,
            "delivery_eta": self.delivery_eta,
            "unverified_charges": self.unverified_charges(),
            "notes": list(self.notes),
        }
