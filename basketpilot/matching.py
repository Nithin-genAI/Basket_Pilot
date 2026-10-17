"""Decide whether a real product actually satisfies a requested list item.

This is the step where a shopping agent most easily lies to its user. A retailer
returns thirty products for "bread"; picking the first one and calling the list
fulfilled is how a basket ends up containing something nobody asked for. So the
decision is made deterministically, scored, and recorded on every offer, and
anything below the confidence threshold is reported as *unavailable* rather than
quietly substituted.

The rules are deliberately conservative about one specific failure: **a colour or
variant word in the request is a requirement, not decoration.** "Brown bread" must
not be satisfied by white bread, and "toned milk" must not be satisfied by full-cream
milk. Token coverage handles this — a product missing a requested token scores below
threshold rather than being waved through on a partial match.

An LLM tier in :mod:`basketpilot.agent` can resolve the genuinely ambiguous cases
this declines to guess at, and marks them ``MATCH_LLM`` when it does.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Product
from .offers import (
    MATCH_BRAND,
    MATCH_CONFIDENCE,
    MATCH_EXACT,
    MATCH_FUZZY,
    MATCH_NAME,
    Item,
    Offer,
    parse_pack_size,
)

# Words too common to carry meaning when comparing a request to a product name.
_NOISE = {
    "fresh", "pack", "packet", "bottle", "bottles", "loaf", "of", "the", "a", "an",
    "and", "with", "for", "new", "best", "buy", "online", "price", "india",
    "g", "gm", "gms", "gram", "grams", "kg", "kgs", "ml", "l", "ltr", "litre",
    "litres", "liter", "liters", "pc", "pcs", "piece", "pieces", "dozen", "unit",
    "units", "x",
}

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# How much of the requested name must appear in the product name. At 1.0 every
# meaningful requested word must be present — which is what makes "brown bread"
# reject "White Bread". Kept as a named constant because it is the single most
# consequential number in this module.
FULL_COVERAGE = 1.0
PARTIAL_COVERAGE = 0.5


@dataclass
class Match:
    """The outcome of matching one product against one item."""

    accepted: bool
    method: str
    confidence: float
    reason: str
    substitution: bool = False
    # Set when the user asked for a specific size and this product is a different one.
    # A 250 ml carton is not a 2 L request, however well the brand and name line up.
    pack_mismatch: bool = False
    pack_note: str = ""


# How far a pack size may differ from the request before it counts as a mismatch.
# A 5% band absorbs the difference between "1 L" and "1000 ml" style rounding without
# admitting a genuinely different pack.
PACK_TOLERANCE = 0.05


def pack_fits(item: Item, product: Product) -> tuple[bool, str]:
    """Does the product's pack size satisfy the size the user asked for?

    Returns ``(fits, note)``. When the item names no size, everything fits — the user
    did not constrain it. When it does name one, a different pack is a mismatch rather
    than a rejection: the product is real and relevant, but buying it would not be
    buying what was asked for, so it needs approval rather than silence.
    """
    if item.quantity is None or item.unit is None:
        return True, ""

    pack_qty, pack_unit = parse_pack_size(product.package_size)
    if pack_unit is None or pack_qty is None:
        # The product's size could not be read. That is not the same as it being wrong,
        # but it also cannot be confirmed, so it is reported rather than assumed.
        return False, (
            f"the pack size of {product.name!r} could not be read, so it cannot be "
            f"confirmed as the {item.quantity:g}{item.unit} requested"
        )

    if pack_unit != item.unit:
        return False, (
            f"pack is measured in {pack_unit}, but the request was in {item.unit}"
        )

    if abs(pack_qty - item.quantity) <= max(item.quantity * PACK_TOLERANCE, 1e-9):
        return True, ""

    return False, (
        f"pack is {product.package_size} but the request was "
        f"{item.quantity:g}{item.unit}"
    )


def tokens(text: str | None) -> set[str]:
    """Meaningful lowercase tokens from a product or item name."""
    if not text:
        return set()
    return {t for t in _TOKEN_RE.findall(text.lower()) if t not in _NOISE and len(t) > 1}


def _brand_matches(item_brand: str | None, product_brand: str | None, product_name: str) -> bool:
    """True when the product's brand is the requested brand.

    Checked against the name as well as the brand field, because retailers are
    inconsistent about populating the latter.
    """
    if not item_brand:
        return False
    needle = item_brand.lower().strip()
    if not needle:
        return False
    if product_brand and needle in product_brand.lower():
        return True
    return needle in (product_name or "").lower()


def match(product: Product, item: Item) -> Match:
    """Score how well ``product`` satisfies ``item``.

    Returns a :class:`Match` whose ``accepted`` flag says whether the product may be
    used at all. A rejected match is not an error — it is the honest answer that this
    product is not what was asked for.
    """
    name = product.name or ""
    product_tokens = tokens(name)
    item_tokens = tokens(item.name)

    brand_ok = _brand_matches(item.brand, product.brand, name)

    # How much of the request the product's name actually covers.
    if item_tokens:
        coverage = len(item_tokens & product_tokens) / len(item_tokens)
    else:
        coverage = 1.0

    # A requested brand that is missing is a hard rejection when the user made it
    # binding, and a strong demotion when they only implied it.
    if item.brand and not brand_ok:
        if item.brand_required:
            return Match(
                False, MATCH_FUZZY, 0.0,
                f"requested brand {item.brand!r} is required but this is "
                f"{product.brand or 'an unbranded product'}",
            )
        if coverage < FULL_COVERAGE:
            return Match(
                False, MATCH_FUZZY, 0.0,
                f"different brand ({product.brand or 'unbranded'}) and the name does "
                f"not fully match {item.name!r}",
            )

    fits, pack_note = pack_fits(item, product)

    if brand_ok and coverage >= FULL_COVERAGE:
        return Match(True, MATCH_EXACT, MATCH_CONFIDENCE[MATCH_EXACT],
                     f"brand {product.brand!r} and full name match",
                     pack_mismatch=not fits, pack_note=pack_note)

    if brand_ok:
        return Match(True, MATCH_BRAND, MATCH_CONFIDENCE[MATCH_BRAND],
                     f"brand {product.brand!r} matches; name partially matches",
                     pack_mismatch=not fits, pack_note=pack_note)

    if coverage >= FULL_COVERAGE:
        return Match(True, MATCH_NAME, MATCH_CONFIDENCE[MATCH_NAME],
                     f"name matches {item.name!r} (no brand requested)",
                     pack_mismatch=not fits, pack_note=pack_note)

    if coverage >= PARTIAL_COVERAGE:
        return Match(True, MATCH_FUZZY, MATCH_CONFIDENCE[MATCH_FUZZY],
                     f"partial name match ({coverage:.0%} of the requested words)",
                     pack_mismatch=not fits, pack_note=pack_note)

    return Match(
        False, MATCH_FUZZY, 0.0,
        f"only {coverage:.0%} of {item.name!r} appears in this product's name",
    )


def best_for_item(product: Product, item: Item) -> Match:
    """Alias kept explicit so call sites read as intent, not as plumbing."""
    return match(product, item)


def satisfies_required_brand(item: Item, offer: Offer) -> tuple[bool, str]:
    """Check a binding brand preference against an offer.

    Exposed as its own function so the optimizer can enforce the constraint itself
    rather than trusting that whoever built the offer ran :func:`match` correctly.
    A brand the user marked as required is a hard constraint on their money, and it
    is worth checking twice — once when the offer is built, and once when it is spent.
    """
    if not item.brand_required or not item.brand:
        return True, ""
    if _brand_matches(item.brand, offer.product.brand, offer.product.name):
        return True, ""
    return False, (
        f"requires brand {item.brand!r} but this is "
        f"{offer.product.brand or 'an unbranded product'}"
    )
