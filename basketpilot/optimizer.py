"""Deterministic basket optimizer. No LLM, no network, no I/O.

Given a shopping list and the real offers retailers returned for it, work out the
best practical way to buy the whole thing — preferring one platform, splitting only
when splitting genuinely helps.

**The LLM never touches this module.** Money is decided here, by arithmetic that can
be read and tested, because "the model suggested a basket" is not an answer anyone
can audit. Every number in the output traces to an observed price or an explicitly
named unknown.

The objective is not "cheapest". It is *complete, then cheap, then simple*:

1. **Coverage first.** A ₹200 basket missing the eggs is worse than a ₹260 basket
   that has everything, so coverage ranks above price. This is the ordering the
   brief asks for when it says to penalise split orders — a split that covers more
   is not a penalty, it is the point.
2. **Then total cost**, including only fees we actually verified.
3. **Then fewest platforms**, via an explicit split penalty, because every extra
   platform is another delivery, another minimum-order risk and another thing the
   user has to approve.

Combinations are enumerated exhaustively. With at most a handful of retailers that is
a few dozen baskets — cheap enough that a heuristic would only add a way to be wrong.

Missing values are never zero. A retailer whose delivery fee we could not verify
contributes no fee to the total *and* marks the whole basket ``estimated``, with the
unknown named in ``unverified_charges``. An estimated basket is never presented as
definitively cheaper than a verified one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from typing import Any, Iterable, Sequence

from .matching import satisfies_required_brand
from .offers import Item, Offer, RetailerQuote

# Statuses a line can have.
LINE_MATCHED = "matched"
LINE_UNAVAILABLE = "unavailable"
LINE_NEEDS_APPROVAL = "needs_approval"

# How a basket's total should be read.
CONFIDENCE_VERIFIED = "verified"
CONFIDENCE_ESTIMATED = "estimated"

# Default penalty, in rupees, for each platform beyond the first. Sized to be
# meaningful against a typical grocery basket: it will not force a split that saves
# ₹300, but it will stop a ₹12 saving from splitting an order across two apps.
DEFAULT_SPLIT_PENALTY = 25.0


@dataclass
class Constraints:
    """What the user will accept. Every field is a real constraint, not a hint."""

    max_platforms: int = 3
    budget: float | None = None
    split_penalty: float = DEFAULT_SPLIT_PENALTY
    # Offers below this confidence are not used at all. 0.55 admits a partial name
    # match and excludes a product that merely shares a word.
    min_match_confidence: float = 0.55
    # When False, a line that can only be satisfied by a substitution is reported for
    # approval rather than being included. The brief requires explicit approval, so
    # this defaults to the safe side.
    allow_substitutions: bool = False
    preferred_retailers: tuple[str, ...] = ()
    # A tie-break nudge, not a rule: preferred retailers win ties but never beat a
    # materially cheaper basket.
    preference_bonus: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_platforms": self.max_platforms,
            "budget": self.budget,
            "split_penalty": self.split_penalty,
            "min_match_confidence": self.min_match_confidence,
            "allow_substitutions": self.allow_substitutions,
            "preferred_retailers": list(self.preferred_retailers),
        }


@dataclass
class BasketLine:
    """One requested item, and how (or whether) a basket satisfies it."""

    item_id: str
    item_name: str
    quantity: float | None = None
    unit: str | None = None
    offer: Offer | None = None
    status: str = LINE_UNAVAILABLE
    reason: str = ""
    alternatives: list[Offer] = field(default_factory=list)

    @property
    def line_total(self) -> float:
        if self.offer is None or self.offer.price is None:
            return 0.0
        return self.offer.price

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "item_name": self.item_name,
            "quantity": self.quantity,
            "unit": self.unit,
            "status": self.status,
            "reason": self.reason,
            "offer": self.offer.to_dict() if self.offer else None,
            "line_total": round(self.line_total, 2),
            "alternatives": [o.to_dict() for o in self.alternatives[:3]],
        }


@dataclass
class Basket:
    """A candidate way to buy the whole list."""

    retailer_keys: tuple[str, ...]
    lines: list[BasketLine] = field(default_factory=list)
    subtotal: float = 0.0
    fees: float = 0.0
    discounts: float = 0.0
    total: float = 0.0
    score: float = 0.0
    coverage: float = 0.0
    covered_items: int = 0
    total_items: int = 0
    cost_confidence: str = CONFIDENCE_ESTIMATED
    unverified_charges: list[str] = field(default_factory=list)
    per_retailer_subtotal: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    over_budget: bool = False

    @property
    def platform_count(self) -> int:
        return len(self.retailer_keys)

    @property
    def missing_items(self) -> list[str]:
        return [line.item_name for line in self.lines if line.status == LINE_UNAVAILABLE]

    @property
    def approval_items(self) -> list[dict[str, str]]:
        """Items held for approval, with the reason — a wrong-size pack, typically."""
        return [
            {"item": line.item_name, "reason": line.reason}
            for line in self.lines
            if line.status == LINE_NEEDS_APPROVAL
        ]

    @property
    def needs_approval(self) -> list[str]:
        return [line.item_name for line in self.lines if line.status == LINE_NEEDS_APPROVAL]

    @property
    def is_complete(self) -> bool:
        return self.covered_items == self.total_items and self.total_items > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "retailers": list(self.retailer_keys),
            "platform_count": self.platform_count,
            "subtotal": round(self.subtotal, 2),
            "fees": round(self.fees, 2),
            "discounts": round(self.discounts, 2),
            "total": round(self.total, 2),
            "score": round(self.score, 2),
            "coverage": round(self.coverage, 4),
            "covered_items": self.covered_items,
            "total_items": self.total_items,
            "complete": self.is_complete,
            "cost_confidence": self.cost_confidence,
            "unverified_charges": list(self.unverified_charges),
            "per_retailer_subtotal": {k: round(v, 2) for k, v in self.per_retailer_subtotal.items()},
            "missing_items": self.missing_items,
            "needs_approval": self.needs_approval,
            "approval_items": self.approval_items,
            "over_budget": self.over_budget,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "lines": [line.to_dict() for line in self.lines],
        }


@dataclass
class OptimizationResult:
    """The recommendation, the runners-up, and everything we could not do."""

    recommended: Basket | None = None
    alternatives: list[Basket] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    needs_approval: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "recommended": self.recommended.to_dict() if self.recommended else None,
            "alternatives": [b.to_dict() for b in self.alternatives],
            "unavailable": list(self.unavailable),
            "needs_approval": list(self.needs_approval),
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------- #
# Offer selection
# --------------------------------------------------------------------------- #


def usable_offers(
    offers: Iterable[Offer],
    constraints: Constraints,
    items: Sequence[Item] | None = None,
) -> list[Offer]:
    """Offers that may be used at all: in stock, priced, confident enough, unsubstituted.

    When ``items`` is supplied, a binding brand preference is enforced *here* as well
    as in :mod:`basketpilot.matching`. That is deliberate duplication of a check, not
    of the logic: the brand rule is a constraint on the user's money, and this module
    should not silently depend on an upstream caller having applied it. The comparison
    itself lives in one place — :func:`basketpilot.matching.satisfies_required_brand`.
    """
    by_id = {item.item_id: item for item in items} if items else {}
    usable: list[Offer] = []
    for offer in offers:
        if offer.price is None:
            # No readable price means no comparable cost. A free-looking product would
            # corrupt every total it entered, so it is excluded rather than zeroed.
            continue
        if offer.match_confidence < constraints.min_match_confidence:
            continue
        if offer.substitution and not constraints.allow_substitutions:
            continue
        if not offer.in_stock:
            continue
        item = by_id.get(offer.item_id)
        if item is not None:
            ok, _ = satisfies_required_brand(item, offer)
            if not ok:
                continue
        usable.append(offer)
    return usable


def cheapest_per_item(
    items: Sequence[Item], offers: Sequence[Offer], retailers: Sequence[str]
) -> tuple[dict[str, Offer], dict[str, list[Offer]]]:
    """For each item, the cheapest usable offer from the given retailers.

    Ties break toward the higher match confidence, then the lower unit price, so the
    choice is stable and explicable rather than dependent on dict ordering.
    """
    chosen: dict[str, Offer] = {}
    considered: dict[str, list[Offer]] = {}
    allowed = set(retailers)

    for item in items:
        candidates = [o for o in offers if o.item_id == item.item_id and o.retailer_key in allowed]
        considered[item.item_id] = sorted(
            candidates,
            key=lambda o: (o.price if o.price is not None else float("inf")),
        )
        if not candidates:
            continue
        # Pack size is ranked *ahead of price*. When the user asked for 2 L, a ₹26
        # 250 ml carton must not beat a ₹60 2 L bottle on price — that is a cheaper
        # product, not a cheaper way to buy what was asked for.
        chosen[item.item_id] = min(
            candidates,
            key=lambda o: (
                1 if o.pack_mismatch else 0,
                o.price if o.price is not None else float("inf"),
                -o.match_confidence,
                o.unit_price if o.unit_price is not None else float("inf"),
            ),
        )
    return chosen, considered


# --------------------------------------------------------------------------- #
# Basket construction
# --------------------------------------------------------------------------- #


def build_basket(
    items: Sequence[Item],
    offers: Sequence[Offer],
    quotes: dict[str, RetailerQuote],
    retailers: Sequence[str],
    constraints: Constraints,
) -> Basket:
    """Assemble the best basket that uses exactly ``retailers``."""
    basket = Basket(retailer_keys=tuple(retailers), total_items=len(items))
    chosen, considered = cheapest_per_item(items, offers, retailers)

    for item in items:
        offer = chosen.get(item.item_id)
        if offer is None:
            # Say *why* it is missing, and distinguish "nobody stocks it" from
            # "everyone stocks it but nothing matched well enough". Those need
            # different responses from the user.
            candidates = [o for o in offers if o.item_id == item.item_id]
            if candidates:
                best = max(candidates, key=lambda o: o.match_confidence)
                reason = (
                    f"{len(candidates)} product(s) found but the best match was only "
                    f"{best.match_confidence:.0%} confident ({best.product.name!r})"
                )
            else:
                reason = "no product found at these retailers"
            basket.lines.append(
                BasketLine(item_id=item.item_id, item_name=item.name,
                           quantity=item.quantity, unit=item.unit,
                           status=LINE_UNAVAILABLE, reason=reason)
            )
            continue

        others = [o for o in considered.get(item.item_id, []) if o is not offer]

        # A pack that is the wrong size is a substitution, and an unapproved
        # substitution is not a fulfilled item. It is shown, priced, and held for the
        # user — never counted as coverage.
        if offer.pack_mismatch and not constraints.allow_substitutions:
            basket.lines.append(
                BasketLine(
                    item_id=item.item_id, item_name=item.name,
                    quantity=item.quantity, unit=item.unit,
                    offer=offer, status=LINE_NEEDS_APPROVAL,
                    reason=(
                        f"only a different size is available: {offer.pack_note}. "
                        "Approval needed before this is bought."
                    ),
                    alternatives=others,
                )
            )
            basket.subtotal += offer.price or 0.0
            basket.per_retailer_subtotal[offer.retailer_key] = (
                basket.per_retailer_subtotal.get(offer.retailer_key, 0.0) + (offer.price or 0.0)
            )
            continue

        basket.lines.append(
            BasketLine(
                item_id=item.item_id, item_name=item.name,
                quantity=item.quantity, unit=item.unit,
                offer=offer, status=LINE_MATCHED,
                reason=offer.match_method, alternatives=others,
            )
        )
        basket.covered_items += 1
        basket.subtotal += offer.price or 0.0
        basket.per_retailer_subtotal[offer.retailer_key] = (
            basket.per_retailer_subtotal.get(offer.retailer_key, 0.0) + (offer.price or 0.0)
        )

    # Fees and discounts, per retailer, using only what was verified.
    for key in retailers:
        quote = quotes.get(key)
        if quote is None:
            basket.unverified_charges.append(f"{key}: no fee information captured")
            continue
        subtotal_here = basket.per_retailer_subtotal.get(key, 0.0)
        basket.fees += quote.verified_fees(subtotal_here)
        basket.discounts += quote.verified_discount()
        for unknown in quote.unverified_charges():
            basket.unverified_charges.append(f"{key}: {unknown}")

    basket.subtotal = round(basket.subtotal, 2)
    basket.fees = round(basket.fees, 2)
    basket.discounts = round(basket.discounts, 2)
    basket.total = round(basket.subtotal + basket.fees - basket.discounts, 2)
    basket.coverage = basket.covered_items / len(items) if items else 0.0
    basket.cost_confidence = (
        CONFIDENCE_VERIFIED if not basket.unverified_charges else CONFIDENCE_ESTIMATED
    )

    # Score: total plus the split penalty. Coverage is deliberately *not* folded in —
    # it is ranked separately and ahead of score, so that a cheap incomplete basket can
    # never outrank a complete one by being cheap.
    penalty = constraints.split_penalty * (basket.platform_count - 1)
    basket.score = round(basket.total + penalty, 2)
    if penalty:
        basket.reasons.append(
            f"split across {basket.platform_count} platforms (+₹{penalty:g} split penalty)"
        )

    if constraints.budget is not None and basket.total > constraints.budget:
        basket.over_budget = True
        basket.warnings.append(
            f"₹{basket.total:g} exceeds the ₹{constraints.budget:g} budget by "
            f"₹{basket.total - constraints.budget:g}"
        )

    return basket


def optimize(
    items: Sequence[Item],
    offers: Sequence[Offer],
    quotes: dict[str, RetailerQuote] | None = None,
    constraints: Constraints | None = None,
    *,
    verified_retailers: Iterable[str] | None = None,
) -> OptimizationResult:
    """Find the best way to buy ``items`` given the ``offers`` observed.

    ``verified_retailers`` is the honesty gate. When supplied, only retailers named in
    it may be used to build a basket — a retailer whose adapter has never completed a
    real run cannot be recommended, however good its numbers look. Passing ``None``
    skips the check, which is what the unit tests do with synthetic fixtures.
    """
    constraints = constraints or Constraints()
    quotes = quotes or {}
    items = list(items)
    result = OptimizationResult()

    if not items:
        result.notes.append("no items to optimize")
        return result

    allowed_retailers = {o.retailer_key for o in offers}
    if verified_retailers is not None:
        verified = set(verified_retailers)
        blocked = allowed_retailers - verified
        if blocked:
            result.notes.append(
                "excluded retailers with no verified live run: "
                + ", ".join(sorted(blocked))
            )
        allowed_retailers &= verified

    usable = usable_offers(offers, constraints, items)
    if not usable:
        result.unavailable = [item.name for item in items]
        result.notes.append(
            "no usable offers: every product was unpriced, out of stock, or matched "
            "below the confidence threshold"
        )
        return result

    # Which retailers actually have something usable. A retailer with nothing cannot
    # be part of any basket, so it is left out of the enumeration entirely.
    candidates = sorted({o.retailer_key for o in usable} & allowed_retailers)
    if not candidates:
        result.unavailable = [item.name for item in items]
        result.notes.append("no verified retailer returned a usable offer")
        return result

    max_platforms = max(1, min(constraints.max_platforms, len(candidates)))

    baskets: list[Basket] = []
    for size in range(1, max_platforms + 1):
        for combo in combinations(candidates, size):
            baskets.append(build_basket(items, usable, quotes, combo, constraints))

    # Rank: complete baskets first, then score. The two-key sort is the whole
    # objective function, stated once.
    def rank(basket: Basket) -> tuple:
        preference = (
            -constraints.preference_bonus
            if basket.retailer_keys and basket.retailer_keys[0] in constraints.preferred_retailers
            else 0.0
        )
        return (
            0 if basket.is_complete else 1,
            basket.score + preference,
        )

    baskets.sort(key=rank)

    best = baskets[0]
    result.recommended = best

    # Alternatives: keep the list short and genuinely different. A basket that covers
    # less and costs more is not an alternative, it is noise.
    for basket in baskets[1:]:
        if len(result.alternatives) >= 3:
            break
        if basket.covered_items < best.covered_items:
            continue
        if basket.total > best.total * 1.5 and not basket.is_complete:
            continue
        result.alternatives.append(basket)

    result.unavailable = best.missing_items
    result.needs_approval = best.needs_approval

    _explain(result, best, baskets, constraints)
    return result


def _explain(
    result: OptimizationResult,
    best: Basket,
    baskets: list[Basket],
    constraints: Constraints,
) -> None:
    """Write the reasons a human can check, in plain language."""
    if best.is_complete:
        if best.platform_count == 1:
            best.reasons.insert(
                0,
                f"{best.retailer_keys[0]} covers all {best.total_items} items on its own, "
                "so no split is needed",
            )
        else:
            best.reasons.insert(
                0,
                f"no single retailer covers all {best.total_items} items, so this "
                f"basket uses {best.platform_count}",
            )
    else:
        # "Not covered" has two very different causes and they need different actions
        # from the user: nothing was found at all, versus something was found but only
        # in the wrong pack size and is waiting for approval. Reporting both as "could
        # not be sourced" would hide a decision the user has to make.
        parts = [f"covers {best.covered_items}/{best.total_items} items"]
        if best.missing_items:
            parts.append(f"{len(best.missing_items)} not stocked: {', '.join(best.missing_items)}")
        if best.needs_approval:
            parts.append(
                f"{len(best.needs_approval)} held for approval (only a different size "
                f"available): {', '.join(best.needs_approval)}"
            )
        best.reasons.insert(0, "; ".join(parts))

    if best.cost_confidence == CONFIDENCE_ESTIMATED:
        best.reasons.append(
            "total is an estimate: " + "; ".join(best.unverified_charges[:3])
        )

    # Compare against the cheapest complete basket that used fewer platforms, so the
    # recommendation can say what the simplicity actually cost.
    simpler = [
        b for b in baskets
        if b.is_complete and b.platform_count < best.platform_count
    ]
    if simpler and best.platform_count > 1:
        cheapest_simpler = min(simpler, key=lambda b: b.score)
        delta = best.score - cheapest_simpler.score
        if delta > 0:
            best.reasons.append(
                f"the split costs ₹{delta:g} more than buying everything from "
                f"{', '.join(cheapest_simpler.retailer_keys)}, but that option does not "
                "cover the full list"
            )

    if best.over_budget:
        result.notes.append(
            "the recommended basket is over budget; consider dropping items or raising "
            "the limit"
        )
