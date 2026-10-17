"""Turn a natural-language shopping list into normalised :class:`Item` records.

The brief's example is *"2 litres of Amul milk, a dozen eggs, brown bread"*, and the
whole point is that a person should be able to type that rather than fill in a form.

This is deterministic first, by design. It handles the shapes people actually write —
quantities with units, brand-first or brand-last, counts spelled as words — without a
network call, which means the parser is unit-testable, the optimizer is testable
without an API key, and the pipeline still works when the LLM is unreachable. An LLM
tier in :mod:`basketpilot.agent` handles genuinely messy input on top of this, and
records that it did so in ``Item.parsed_by``.

Nothing here invents a quantity. "milk" parses to an item with no quantity rather
than to "1 milk", because a fabricated quantity would flow straight into the
optimizer as a constraint the user never set.
"""

from __future__ import annotations

import re

from .offers import UNIT_ALIASES, Item, parse_pack_size

# Words people write for quantities. Mapped to (number, unit) so "a dozen eggs"
# becomes 12 count and "half a kilo" becomes 500 g.
WORD_QUANTITIES: dict[str, tuple[float, str | None]] = {
    "a": (1, None),
    "an": (1, None),
    "one": (1, None),
    "two": (2, None),
    "three": (3, None),
    "four": (4, None),
    "five": (5, None),
    "six": (6, None),
    "seven": (7, None),
    "eight": (8, None),
    "nine": (9, None),
    "ten": (10, None),
    "half": (0.5, None),
    "dozen": (12, "count"),
    "couple": (2, None),
}

# Filler that carries no meaning for a product search.
STOPWORDS = {
    "of", "some", "the", "please", "buy", "get", "need", "want", "add",
    "and", "fresh", "pack", "packet", "bottle", "bottles", "loaf",
}

# Leading quantity: "2", "2L", "500 g", "1.5 kg", "2 litres".
# The unit class is generous on purpose — "litres" is six letters and a tighter bound
# silently truncated it, leaving "litres" in the product name.
_LEADING_QTY_RE = re.compile(
    r"^\s*(?P<qty>\d+(?:\.\d+)?)\s*(?P<unit>[a-zA-Z]{1,10})?\b\s*(?:of\s+)?",
)
_LEADING_WORD_RE = re.compile(r"^\s*(?P<word>[a-zA-Z]+)\s+(?:of\s+)?")

# A brand is recognised from how it is capitalised mid-string: "2L Amul milk" has
# "Amul" as the only capitalised token that is not the first word. This is a
# heuristic and is labelled as one — `brand_required` is only set when the user's
# phrasing makes the brand a constraint ("Amul milk", "only Amul").
_ALLCAPS_OR_TITLE_RE = re.compile(r"^[A-Z][A-Za-z&'.]*$")

# Words that signal the brand is mandatory rather than a preference.
_REQUIRED_MARKERS = ("only", "must be", "specifically", "exactly")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "item"


def _bare_unit(word: str) -> tuple[float, str] | None:
    """Look up a unit written without a number: "kg", "litres", "pcs".

    :func:`~basketpilot.offers.parse_pack_size` cannot help here — it requires a
    digit — but "half kg" needs the unit parsed on its own.
    """
    entry = UNIT_ALIASES.get(word.lower().strip(".,"))
    if not entry:
        return None
    base_unit, factor = entry
    return factor, base_unit


def _clean_name(text: str) -> str:
    words = [w for w in re.split(r"\s+", text.strip()) if w]
    kept = [w for w in words if w.lower() not in STOPWORDS]
    return " ".join(kept) if kept else " ".join(words)


def _split_list(raw: str) -> list[str]:
    """Split on commas, semicolons and newlines — the separators people actually use."""
    parts = re.split(r"[,;\n]+", raw)
    return [p.strip() for p in parts if p.strip()]


def parse_item(raw: str, index: int = 0) -> Item:
    """Parse one list line into an :class:`Item`."""
    original = raw.strip()
    text = original

    # A required-brand marker anywhere in the line makes the brand a constraint.
    lowered = text.lower()
    brand_required = any(marker in lowered for marker in _REQUIRED_MARKERS)
    for marker in _REQUIRED_MARKERS:
        text = re.sub(rf"\b{re.escape(marker)}\b", " ", text, flags=re.IGNORECASE)

    # Brand detection reads the line *before* the quantity is stripped. After
    # stripping, "2L Amul milk" becomes "Amul milk" and the brand sits in first
    # position, where the "a capitalised word that is not the first word" rule would
    # miss it.
    brand_source = text

    quantity: float | None = None
    unit: str | None = None

    # 1. A leading numeric quantity, possibly with a unit: "2L milk", "500 g paneer".
    match = _LEADING_QTY_RE.match(text)
    if match:
        quantity = float(match.group("qty"))
        if match.group("unit"):
            # Reuse the pack-size parser so "2L" and "2 litres" normalise identically.
            parsed_qty, parsed_unit = parse_pack_size(match.group(0).strip())
            if parsed_unit:
                quantity, unit = parsed_qty, parsed_unit
        text = text[match.end():]

    # 2. A leading word quantity: "a dozen eggs", "two onions", "half kg paneer".
    if quantity is None:
        word_match = _LEADING_WORD_RE.match(text)
        if word_match and word_match.group("word").lower() in WORD_QUANTITIES:
            word = word_match.group("word").lower()
            base_qty, base_unit = WORD_QUANTITIES[word]
            remainder = text[word_match.end():]

            # The next word may itself be a quantity ("a dozen") or a bare unit
            # ("half kg"). Both multiply the leading word rather than replacing it.
            inner = _LEADING_WORD_RE.match(remainder)
            inner_word = inner.group("word").lower() if inner else ""
            if inner and inner_word in WORD_QUANTITIES:
                inner_qty, inner_unit = WORD_QUANTITIES[inner_word]
                quantity = base_qty * inner_qty
                unit = inner_unit or base_unit
                text = remainder[inner.end():]
            else:
                bare = _bare_unit(remainder.split()[0]) if remainder.split() else None
                if bare and base_qty != 1:
                    factor, bare_unit_name = bare
                    quantity = base_qty * factor
                    unit = bare_unit_name
                    text = " ".join(remainder.split()[1:])
                else:
                    quantity, unit = base_qty, base_unit
                    text = remainder

    # 3. A trailing size is the *pack* the user wants, which is a preference, not a
    #    count of items. "milk 1L" means one pack of a litre, not one litre total.
    if quantity is None:
        trailing = parse_pack_size(text)
        if trailing[1]:
            # Leave it in the name so the search query still carries it; the matcher
            # uses it to prefer the right pack size.
            quantity, unit = None, trailing[1]

    name = _clean_name(text)
    brand = _extract_brand(brand_source)

    # The name should not repeat the brand, or the search query says it twice.
    if brand and name.lower().startswith(brand.lower()):
        name = name[len(brand):].strip() or name

    return Item(
        item_id=f"{_slug(name)}-{index}",
        name=name or original,
        raw=original,
        quantity=quantity,
        unit=unit,
        brand=brand,
        brand_required=brand_required,
        parsed_by="deterministic",
    )


def _extract_brand(text: str) -> str | None:
    """Best-effort brand extraction from capitalisation.

    Only used to *narrow the search query*. It never sets ``brand_required``, because
    capitalisation alone is weak evidence — "Brown Bread" is capitalised and is not a
    brand. The marker words are what make a brand binding.
    """
    words = [w.strip(".,") for w in re.split(r"\s+", text.strip()) if w]

    def meaningful(word: str) -> bool:
        lowered = word.lower()
        return bool(word) and lowered not in STOPWORDS and lowered not in UNIT_ALIASES

    # A brand is only a brand when there is a product noun beside it. "Milk" on its own
    # is a product, not the brand "Milk"; "Amul milk" is a brand and a product. Without
    # this check, capitalising the first word of an item would invent a brand and split
    # "milk, milk, Milk" into two different items.
    if sum(1 for w in words if meaningful(w)) < 2:
        return None

    for word in words:
        if not word or not _ALLCAPS_OR_TITLE_RE.match(word):
            continue
        lowered = word.lower()
        if lowered in STOPWORDS or lowered in WORD_QUANTITIES or lowered in UNIT_ALIASES:
            continue
        # The first capitalised word that is not filler. Position is deliberately not
        # considered: "Amul milk" and "2L Amul milk" both have the brand adjacent to
        # the product noun, and by the time this runs the quantity is still present, so
        # a position rule would only add a way to miss it.
        return word
    return None


def parse_list(raw: str) -> list[Item]:
    """Parse a whole shopping list into items, preserving order and dropping dupes."""
    items: list[Item] = []
    seen: set[str] = set()
    for index, line in enumerate(_split_list(raw)):
        item = parse_item(line, index=len(items))
        key = f"{item.name.lower()}|{item.brand or ''}".strip()
        if key in seen:
            continue
        seen.add(key)
        items.append(item)
    return items


def to_queries(items: list[Item]) -> list[str]:
    """One search query per item, de-duplicated — what the retailer is asked for."""
    queries: list[str] = []
    for item in items:
        query = item.search_query
        if query and query not in queries:
            queries.append(query)
    return queries
