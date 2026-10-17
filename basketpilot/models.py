"""Data model and validation for BasketPilot.

The governing rule of this module is that **no field is ever invented**. A price
is only recorded if it was actually observed on the page, and the numeric price is
derived here from the displayed string rather than trusted from an agent. Where the
two disagree, the disagreement is recorded as a warning instead of being silently
reconciled.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

AVAILABILITY_VALUES = ("in_stock", "out_of_stock", "unknown")

# Matches an Indian-format price such as "₹28", "₹ 1,299.50", "Rs.45", "MRP: ₹99".
# Deliberately anchored on a currency marker so a pack size like "500 ml" or a
# quantity like "1 kg" can never be mistaken for a price.
_PRICE_RE = re.compile(
    r"(?:₹|rs\.?|inr)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
    re.IGNORECASE,
)


def parse_price_displayed(displayed: str | None) -> float | None:
    """Extract a numeric price from a displayed price string.

    Returns ``None`` when no currency-marked number is present. We parse the
    displayed string ourselves, rather than trusting an agent-supplied number,
    so that the numeric price always traces to something a human could read.
    """
    if not displayed:
        return None
    match = _PRICE_RE.search(displayed)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def _evidence_supports(evidence: str, price: float) -> bool:
    """Whether a quoted piece of on-screen text actually contains the given price.

    Compares the numeric value, so "₹28.00" corroborates 28.0 and "Rs. 1,299"
    corroborates 1299.0. Any number appearing in the evidence that equals the price
    counts, which tolerates surrounding text like "MRP ₹30 ₹28".
    """
    for match in re.finditer(r"[0-9][0-9,]*(?:\.[0-9]{1,2})?", evidence):
        try:
            if abs(float(match.group(0).replace(",", "")) - price) <= 0.01:
                return True
        except ValueError:
            continue
    return False


def _clean(value: Any) -> str | None:
    """Normalise an optional string field, treating blanks and placeholders as absent."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    # Agents asked for null sometimes return the word instead. Treat as absent
    # rather than recording a literal "null" as a brand name.
    if text.lower() in {"null", "none", "n/a", "na", "unknown", "-", "--"}:
        return None
    return text


@dataclass
class Product:
    """One product observed on a retailer page."""

    query: str
    name: str
    availability: str = "unknown"
    brand: str | None = None
    package_size: str | None = None
    price_displayed: str | None = None
    price_numeric: float | None = None
    currency: str | None = None
    product_url: str | None = None
    # Verbatim text the agent claims it read off the screen. This is what makes
    # anti-fabrication checkable rather than merely promised: we verify the digits
    # of the price actually appear in this string.
    evidence: str | None = None
    # Which rung produced this record: tinyfish | cloak_json | cloak_dom | ...
    source: str | None = None
    warnings: list[str] = field(default_factory=list)

    def validate(self) -> list[str]:
        """Check internal consistency, returning warnings. Mutates ``self`` to fill
        in ``price_numeric`` from ``price_displayed``.

        This is the anti-fabrication gate. A price survives only if it was displayed
        *and*, where evidence was supplied, corroborated by that evidence.

        Any warnings already on the record are kept: extractors attach provenance
        there (source URL, product id, stock count), and losing that would make a
        validated row less auditable than an unvalidated one.
        """
        warnings: list[str] = list(self.warnings)

        self.query = _clean(self.query) or ""
        self.name = _clean(self.name) or ""
        self.brand = _clean(self.brand)
        self.package_size = _clean(self.package_size)
        self.price_displayed = _clean(self.price_displayed)
        self.product_url = _clean(self.product_url)
        self.currency = _clean(self.currency)
        self.evidence = _clean(self.evidence)
        self.source = _clean(self.source)

        if not self.name:
            warnings.append("rejected: product has no name")

        if self.availability not in AVAILABILITY_VALUES:
            warnings.append(
                f"availability {self.availability!r} not in {AVAILABILITY_VALUES}; set to 'unknown'"
            )
            self.availability = "unknown"

        # The displayed string is the source of truth, not any agent-supplied number.
        derived = parse_price_displayed(self.price_displayed)

        if derived is None and self.price_numeric is not None:
            warnings.append(
                f"dropped untraceable price_numeric={self.price_numeric!r}: "
                "no currency-marked price in price_displayed"
            )
            self.price_numeric = None
        elif derived is not None and self.price_numeric is None:
            self.price_numeric = derived
        elif derived is not None and self.price_numeric is not None:
            if abs(derived - float(self.price_numeric)) > 0.01:
                warnings.append(
                    f"price mismatch: agent reported {self.price_numeric!r} "
                    f"but displayed text parses to {derived!r}; using displayed"
                )
            self.price_numeric = derived

        # Corroborate the price against the quoted on-screen evidence.
        if self.price_numeric is not None and self.evidence:
            if not _evidence_supports(self.evidence, self.price_numeric):
                warnings.append(
                    f"dropped price {self.price_numeric!r}: it does not appear in the "
                    f"quoted evidence {self.evidence!r}"
                )
                self.price_numeric = None
                self.price_displayed = None

        if self.price_numeric is None and self.availability == "in_stock":
            warnings.append("in_stock but no price captured")

        if self.product_url and not self.product_url.startswith(("http://", "https://")):
            warnings.append(f"discarded non-absolute product_url {self.product_url!r}")
            self.product_url = None

        self.warnings = warnings
        return warnings

    @property
    def is_valid(self) -> bool:
        return bool(self.name)

    @property
    def is_usable(self) -> bool:
        """Whether this record is useful enough to count toward coverage.

        A name alone is not enough — a row with no price and no explicit
        out-of-stock marker usually means we scraped a skeleton, not a result.
        """
        if not self.is_valid:
            return False
        if self.price_numeric is not None:
            return True
        return self.availability == "out_of_stock"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RetailerResult:
    """The outcome of one engine run against one retailer."""

    retailer: str
    engine: str
    queries: list[str]
    status: str
    delivery_pincode: str | None = None
    products: list[Product] = field(default_factory=list)
    error_code: str | None = None
    error_category: str | None = None
    error_message: str | None = None
    run_id: str | None = None
    num_of_steps: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    request_id: str | None = None
    warnings: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def usable_products(self) -> list[Product]:
        return [product for product in self.products if product.is_usable]

    @property
    def succeeded(self) -> bool:
        """A run only counts as a success if it produced at least one usable product.

        TinyFish reports ``COMPLETED`` when the *infrastructure* worked, which is not
        the same as the goal having been achieved. Treating "completed with zero
        products" as success would let a silent block masquerade as a pass.
        """
        return self.status == "COMPLETED" and bool(self.usable_products)

    @property
    def diagnosis(self) -> str:
        """A human-readable explanation of why a run did not succeed."""
        if self.succeeded:
            return f"{len(self.usable_products)} usable product(s) captured."
        if self.status == "COMPLETED" and not self.usable_products:
            if self.products:
                return (
                    f"Run completed and returned {len(self.products)} product row(s), but "
                    "none carried a price or an out-of-stock marker — the page was "
                    "probably a skeleton or a bot challenge rather than real results."
                )
            return (
                "Run completed but extracted zero products — the page was probably a "
                "bot challenge or an empty catalogue rather than real search results."
            )
        if self.error_code:
            return f"{self.error_code}: {self.error_message or 'no message'}"
        return f"Run ended with status {self.status} and no diagnosable error."

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["succeeded"] = self.succeeded
        data["diagnosis"] = self.diagnosis
        return data
