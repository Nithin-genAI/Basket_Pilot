"""The parent shopping agent.

One agent, not a swarm. It takes a shopping list, decides what to search for, runs
the retailers, turns what comes back into offers, hands those to the deterministic
optimizer, and explains the result. The brief is explicit that this is a single
parent agent that delegates browser work to tools rather than several autonomous
agents negotiating with each other, and that shape is also the one that can be
debugged under a deadline.

**Two tiers, and the deterministic one is not a fallback — it is the default.**

* The deterministic tier parses lists, matches products and writes explanations with
  no network call. It is what runs today.
* The LLM tier (:mod:`basketpilot.llm`, Fireworks) handles genuinely messy list input
  and ambiguous product matches when a key is configured. It is additive: every call
  returns ``None`` on failure and the deterministic path continues.

**The LLM never decides money.** It may split "2L Amul milk, a dozen eggs" into items;
it may not choose a basket. Baskets come from :mod:`basketpilot.optimizer`, which is
pure arithmetic. This is the difference between a recommendation that can be audited
and one that cannot.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import adapters, llm
from .config import Settings
from .listparse import parse_list
from .models import RetailerResult
from .offers import Item, Offer, RetailerQuote
from .optimizer import Constraints, OptimizationResult, optimize


@dataclass
class AgentRun:
    """Everything the agent did, in the order it did it."""

    items: list[Item] = field(default_factory=list)
    results: dict[str, RetailerResult] = field(default_factory=dict)
    offers: list[Offer] = field(default_factory=list)
    quotes: dict[str, RetailerQuote] = field(default_factory=dict)
    optimization: OptimizationResult | None = None
    retailer_status: dict[str, dict[str, Any]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
            "retailers": self.retailer_status,
            "offer_count": len(self.offers),
            "offers": [offer.to_dict() for offer in self.offers],
            "quotes": {k: q.to_dict() for k, q in self.quotes.items()},
            "optimization": self.optimization.to_dict() if self.optimization else None,
            "notes": list(self.notes),
        }


class ShoppingAgent:
    """The parent agent. One instance drives one shopping list."""

    def __init__(
        self,
        settings: Settings,
        *,
        verbose: bool = True,
        use_llm: bool | None = None,
    ) -> None:
        self.settings = settings
        self.verbose = verbose
        # `use_llm=None` means "use it if a key exists". An explicit False is honoured
        # even with a key present, so the deterministic path stays testable.
        self.use_llm = llm.available() if use_llm is None else use_llm

    def log(self, message: str) -> None:
        if self.verbose:
            print(f"  [agent] {message}", file=sys.stderr, flush=True)

    # ----------------------------------------------------------------- #
    # 1. Understand the list
    # ----------------------------------------------------------------- #

    def parse(self, raw_list: str) -> list[Item]:
        """Turn the user's text into items. Deterministic first, LLM only if needed."""
        items = parse_list(raw_list)
        self.log(f"parsed {len(items)} item(s) deterministically")

        if not items:
            self.log("the deterministic parser found nothing; trying the LLM tier")
            items = self._parse_with_llm(raw_list)
            if items:
                self.log(f"the LLM tier recovered {len(items)} item(s)")

        if self.use_llm and items and self._looks_messy(raw_list):
            enriched = self._parse_with_llm(raw_list)
            # Only adopt the LLM's reading when it found strictly more structure.
            # Letting it replace a good deterministic parse would make the pipeline's
            # behaviour depend on a remote model for no gain.
            if enriched and self._is_richer(enriched, items):
                self.log("the LLM tier found more structure; using its parse")
                items = enriched
        return items

    @staticmethod
    def _looks_messy(raw: str) -> bool:
        """Heuristic for when the LLM tier is worth a call at all."""
        if len(raw) > 200:
            return True
        # Sentences with no commas are the shape people write when they are not
        # thinking in list form: "i need milk and some bread and maybe eggs".
        return raw.count(",") == 0 and len(raw.split()) > 8

    @staticmethod
    def _is_richer(candidate: list[Item], current: list[Item]) -> bool:
        def structure(items: list[Item]) -> int:
            return sum(1 for i in items if i.quantity is not None) + sum(
                1 for i in items if i.brand
            )
        return len(candidate) >= len(current) and structure(candidate) > structure(current)

    def _parse_with_llm(self, raw_list: str) -> list[Item]:
        from .listparse import parse_item

        if not self.use_llm:
            return []
        payload = llm.complete_json(
            "Split this shopping list into items. Return ONLY a JSON array, where each "
            'element is {"name": string, "quantity": number or null, "unit": one of '
            '"g","ml","count" or null, "brand": string or null}. Use the unit "g" for '
            "weights and \"ml\" for volumes. Do not invent quantities or brands that "
            f"are not in the text.\n\nShopping list: {raw_list}",
            system="You extract structured data from shopping lists. Output JSON only.",
        )
        if not isinstance(payload, list):
            return []

        items: list[Item] = []
        for index, entry in enumerate(payload):
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            item = parse_item(str(entry["name"]), index=index)
            # The LLM may supply quantity/unit/brand that the deterministic parse of the
            # name alone would miss, so take them — but only when they are well-formed.
            quantity = entry.get("quantity")
            if isinstance(quantity, (int, float)) and quantity > 0:
                item.quantity = float(quantity)
            unit = entry.get("unit")
            if unit in ("g", "ml", "count"):
                item.unit = unit
            brand = entry.get("brand")
            if isinstance(brand, str) and brand.strip():
                item.brand = brand.strip()
            item.parsed_by = "llm"
            items.append(item)
        return items

    # ----------------------------------------------------------------- #
    # 2. Decide where to search
    # ----------------------------------------------------------------- #

    def plan_retailers(self, items: list[Item], requested: list[str] | None = None) -> list[adapters.Retailer]:
        """Which retailers to search, and in what order.

        Verified retailers go first: spending browser time on a retailer that has never
        produced a product, while a proven one waits, is the wrong trade under a
        deadline. Unverified ones are still attempted, because the brief asks for
        coverage — they are just not allowed to delay the working path.
        """
        adapters.load_ledger()
        if requested:
            chosen = [adapters.get(key) for key in requested]
        else:
            chosen = adapters.all_retailers()

        verified = [r for r in chosen if r.verification.is_verified]
        unverified = [r for r in chosen if not r.verification.is_verified]
        ordered = verified + unverified

        self.log(
            "search order: "
            + ", ".join(
                f"{r.name}({'verified' if r.verification.is_verified else 'unverified'})"
                for r in ordered
            )
        )
        return ordered

    # ----------------------------------------------------------------- #
    # 3. Search
    # ----------------------------------------------------------------- #

    def search_retailers(
        self,
        retailers: list[adapters.Retailer],
        items: list[Item],
        *,
        pincode: str,
        engine: str = "auto",
        screenshot_dir: Path | None = None,
        country: str | None = "IN",
        profile_id: str | None = None,
        on_result: Callable[[str, RetailerResult], None] | None = None,
    ) -> dict[str, RetailerResult]:
        """Search each retailer in turn, collecting results and failures alike."""
        results: dict[str, RetailerResult] = {}
        for retailer in retailers:
            self.log(f"searching {retailer.name}…")
            try:
                result = adapters.search(
                    retailer,
                    items=items,
                    pincode=pincode,
                    engine=engine,
                    settings=self.settings,
                    verbose=self.verbose,
                    screenshot_dir=screenshot_dir,
                    country=country,
                    profile_id=profile_id,
                )
            except Exception as exc:  # noqa: BLE001 - one retailer must not end the run
                result = RetailerResult(
                    retailer=retailer.name,
                    engine=engine,
                    queries=[i.search_query for i in items],
                    status="FAILED",
                    error_code="ADAPTER_ERROR",
                    error_message=f"{type(exc).__name__}: {exc}",
                )
            results[retailer.key] = result
            if on_result:
                on_result(retailer.key, result)
            self.log(
                f"{retailer.name}: {result.status}, {len(result.usable_products)} usable "
                f"product(s)"
            )
        return results

    # ----------------------------------------------------------------- #
    # 4. Build offers and quotes
    # ----------------------------------------------------------------- #

    def build_offers(
        self, items: list[Item], results: dict[str, RetailerResult]
    ) -> list[Offer]:
        """Turn every retailer's products into matched offers."""
        offers: list[Offer] = []
        for key, result in results.items():
            if not result.products:
                continue
            found = adapters.offers_from_result(result, items, retailer_key=key)
            self.log(f"{result.retailer}: {len(found)} offer(s) matched to list items")
            offers.extend(found)
        return offers

    def build_quotes(self, results: dict[str, RetailerResult]) -> dict[str, RetailerQuote]:
        """Fee information, which today comes from the search page only.

        Nothing here is guessed. Where a retailer's fees were not captured, the quote
        stays empty and the optimizer marks the resulting total an estimate — the
        brief's "do not treat missing values as zero", applied at the point it bites.
        """
        quotes: dict[str, RetailerQuote] = {}
        for key, result in results.items():
            quote = RetailerQuote(retailer_key=key, retailer=result.retailer)
            captured = result.raw.get("fees")
            if isinstance(captured, dict):
                for field_name in (
                    "delivery_fee", "free_delivery_threshold", "handling_fee",
                    "small_cart_fee", "discount", "min_order", "delivery_eta",
                ):
                    if field_name in captured:
                        setattr(quote, field_name, captured[field_name])
                for flag in (
                    "delivery_fee_verified", "handling_fee_verified",
                    "small_cart_fee_verified", "discount_verified",
                ):
                    if flag in captured:
                        setattr(quote, flag, bool(captured[flag]))
            else:
                quote.notes.append(
                    "fees were not captured from the search page, so this retailer's "
                    "total is a floor rather than a final amount"
                )
            quotes[key] = quote
        return quotes

    # ----------------------------------------------------------------- #
    # 5. Optimize and explain
    # ----------------------------------------------------------------- #

    def optimize(
        self,
        items: list[Item],
        offers: list[Offer],
        quotes: dict[str, RetailerQuote],
        constraints: Constraints | None = None,
    ) -> OptimizationResult:
        """Hand the offers to the deterministic optimizer.

        The verification gate is passed explicitly: only retailers with a recorded
        successful run may form a basket, so a retailer cannot be recommended on the
        strength of numbers nobody has ever confirmed are reachable.
        """
        return optimize(
            items,
            offers,
            quotes,
            constraints,
            verified_retailers=adapters.verified_keys(),
        )

    def explain(self, run: AgentRun) -> str:
        """A plain-language summary. Templated deterministically, optionally polished."""
        base = self._explain_deterministic(run)
        if not self.use_llm or run.optimization is None or run.optimization.recommended is None:
            return base
        polished = llm.complete(
            "Rewrite this grocery basket summary for a shopper. Keep every number, "
            "retailer name and caveat exactly as given. Do not add any claim that is "
            f"not in the text.\n\n{base}",
            system="You rewrite text faithfully. You never invent facts or numbers.",
            max_tokens=600,
        )
        # The deterministic text is used unless the rewrite actually returned. A
        # missing summary would be a worse outcome than an unpolished one.
        return polished.strip() if polished and polished.strip() else base

    def _explain_deterministic(self, run: AgentRun) -> str:
        lines: list[str] = []
        opt = run.optimization
        if opt is None or opt.recommended is None:
            lines.append("No basket could be built from the offers that came back.")
            for note in (opt.notes if opt else []):
                lines.append(f"  - {note}")
            return "\n".join(lines)

        best = opt.recommended
        lines.append(
            f"Recommended: buy from {', '.join(best.retailer_keys)} "
            f"({best.platform_count} platform{'s' if best.platform_count != 1 else ''})."
        )
        lines.append(
            f"  goods ₹{best.subtotal:g}"
            + (f" + fees ₹{best.fees:g}" if best.fees else "")
            + (f" − discounts ₹{best.discounts:g}" if best.discounts else "")
            + f" = ₹{best.total:g}"
        )
        lines.append(
            f"  covers {best.covered_items}/{best.total_items} items "
            f"({best.coverage:.0%}); total is {best.cost_confidence}"
        )
        for reason in best.reasons:
            lines.append(f"  · {reason}")
        if best.unverified_charges:
            lines.append("  unverified: " + "; ".join(best.unverified_charges))
        if best.missing_items:
            lines.append(f"  could not source: {', '.join(best.missing_items)}")
        if opt.alternatives:
            lines.append("Alternatives:")
            for alt in opt.alternatives:
                lines.append(
                    f"  - {', '.join(alt.retailer_keys)}: ₹{alt.total:g} "
                    f"({alt.covered_items}/{alt.total_items} items, {alt.cost_confidence})"
                )
        return "\n".join(lines)

    # ----------------------------------------------------------------- #
    # The whole run
    # ----------------------------------------------------------------- #

    def run(
        self,
        raw_list: str,
        *,
        pincode: str,
        engine: str = "auto",
        retailers: list[str] | None = None,
        constraints: Constraints | None = None,
        screenshot_dir: Path | None = None,
        country: str | None = "IN",
        profile_id: str | None = None,
        on_result: Callable[[str, RetailerResult], None] | None = None,
    ) -> AgentRun:
        """List in, ranked recommendation out."""
        run = AgentRun()
        run.notes.append(
            "LLM tier " + ("enabled (Fireworks)" if self.use_llm else "not configured; deterministic only")
        )

        run.items = self.parse(raw_list)
        if not run.items:
            run.notes.append("no items could be parsed from the list")
            return run

        chosen = self.plan_retailers(run.items, retailers)
        run.results = self.search_retailers(
            chosen,
            run.items,
            pincode=pincode,
            engine=engine,
            screenshot_dir=screenshot_dir,
            country=country,
            profile_id=profile_id,
            on_result=on_result,
        )

        for key, result in run.results.items():
            run.retailer_status[key] = {
                "name": result.retailer,
                "engine": result.engine,
                "status": result.status,
                "products": len(result.products),
                "usable": len(result.usable_products),
                "error_code": result.error_code,
                "error_message": result.error_message,
                "run_id": result.run_id,
                "diagnosis": result.diagnosis,
                "verified": adapters.get(key).verification.is_verified,
                "verified_engine": adapters.get(key).verification.verified_engine,
                "verified_at": adapters.get(key).verification.verified_at,
            }

        run.offers = self.build_offers(run.items, run.results)
        run.quotes = self.build_quotes(run.results)
        run.optimization = self.optimize(run.items, run.offers, run.quotes, constraints)

        if not run.offers:
            run.notes.append(
                "no retailer returned a usable offer, so there is nothing to optimize. "
                "The per-retailer diagnostics above say what each one did."
            )
        return run
