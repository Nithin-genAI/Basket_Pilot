"""Retailer registry, the verification ledger, and the one adapter that drives them.

Every target retailer is the same shape: a search URL, a delivery-location gate, and
a page whose products come out through the same extraction ladder. So there is one
adapter, parameterised by a :class:`Retailer` record, rather than six near-identical
classes. The differences that genuinely exist — which engine reaches it, whether a
login is needed, what blocked it — are data, and they live in the registry.

**Verification is earned, not declared.** The brief forbids calling a retailer
"working" until a real browser execution has succeeded, so that rule is enforced in
code: :class:`Verification` is populated only by :func:`record_success`, which is
called only from a live run that produced usable products, and the ledger is written
to disk so the claim survives the process that made it. A retailer with no entry is
``UNVERIFIED`` and the optimizer refuses to build a basket on it.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .models import Product, RetailerResult
from .offers import Item, Offer, offer_from_product
from .matching import match

# Engines, in the order we prefer them. TinyFish first: it is the required execution
# layer for Phase 2, and the local browser is an explicitly-labelled fallback rather
# than a silent substitute.
ENGINE_TINYFISH_BROWSER = "tinyfish_browser"
ENGINE_TINYFISH_AGENT = "tinyfish_agent"
ENGINE_CLOAK = "cloak"

LEDGER_PATH = Path("runs") / "verification.json"


@dataclass
class Verification:
    """Evidence that a retailer actually works. Empty means it never has."""

    verified_at: str | None = None
    verified_run_id: str | None = None
    verified_engine: str | None = None
    products_seen: int = 0
    blocker: str | None = None

    @property
    def is_verified(self) -> bool:
        # A timestamp alone is not enough: a run that produced no products proves the
        # site was reachable, not that it is usable.
        return bool(self.verified_at) and self.products_seen > 0

    @property
    def status(self) -> str:
        return "VERIFIED" if self.is_verified else "UNVERIFIED"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status
        return payload


@dataclass
class Retailer:
    """One target retailer."""

    key: str
    name: str
    url: str
    search_url: str
    needs_auth: bool = False
    note: str = ""
    verification: Verification = field(default_factory=Verification)

    def search_url_for(self, query: str) -> str:
        from urllib.parse import quote

        return self.search_url.format(query=quote(query))


# The six targets from the brief, in the order it lists them. Only Blinkit carries a
# verification record today, and it is written by a live run rather than by hand —
# see ``tools/probe_browser.py`` and the README for the run that earned it.
RETAILERS: dict[str, Retailer] = {
    "blinkit": Retailer(
        key="blinkit",
        name="Blinkit",
        url="https://blinkit.com/",
        search_url="https://blinkit.com/s/?q={query}",
        note=(
            "Cloudflare-protected. Reached by the TinyFish Browser API behind an Indian "
            "residential proxy; the Agent API is blocked."
        ),
    ),
    "zepto": Retailer(
        key="zepto",
        name="Zepto",
        url="https://www.zepto.com/",
        search_url="https://www.zepto.com/search?query={query}",
        note="TinyFish Fetch reports bot_blocked on the search page.",
    ),
    "jiomart": Retailer(
        key="jiomart",
        name="JioMart",
        url="https://www.jiomart.com/",
        search_url="https://www.jiomart.com/search/{query}",
        note="Fetch reaches the page but returns an app shell, not products.",
    ),
    "instamart": Retailer(
        key="instamart",
        name="Swiggy Instamart",
        url="https://www.swiggy.com/instamart",
        search_url="https://www.swiggy.com/instamart/search?query={query}",
        note="TinyFish Fetch reports bot_blocked on the search page.",
    ),
    "bigbasket": Retailer(
        key="bigbasket",
        name="BigBasket",
        url="https://www.bigbasket.com/",
        search_url="https://www.bigbasket.com/ps/?q={query}",
        note="Fetch returns navigation chrome only; products are rendered client-side.",
    ),
    "flipkart": Retailer(
        key="flipkart",
        name="Flipkart Grocery",
        url="https://www.flipkart.com/",
        search_url="https://www.flipkart.com/search?q={query}&marketplace=GROCERY",
        note="Fetch returns an empty shell for the grocery search page.",
    ),
}

# The order the brief asks for: proven first, then attempted, then documented.
PRIORITY_ORDER = ("blinkit", "zepto", "jiomart", "instamart", "bigbasket", "flipkart")


class UnknownRetailer(ValueError):
    """Raised when a retailer has no registry entry."""


def get(key: str) -> Retailer:
    try:
        return RETAILERS[key.lower()]
    except KeyError:
        known = ", ".join(PRIORITY_ORDER)
        raise UnknownRetailer(f"Unknown retailer {key!r}. Known retailers: {known}") from None


def all_retailers() -> list[Retailer]:
    return [RETAILERS[key] for key in PRIORITY_ORDER]


# --------------------------------------------------------------------------- #
# The verification ledger
# --------------------------------------------------------------------------- #


def load_ledger(path: Path | None = None) -> None:
    """Load recorded verifications into the registry, if the ledger exists."""
    ledger = path or LEDGER_PATH
    if not ledger.exists():
        return
    try:
        payload = json.loads(ledger.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    for key, entry in payload.items():
        retailer = RETAILERS.get(key)
        if retailer is None or not isinstance(entry, dict):
            continue
        retailer.verification = Verification(
            verified_at=entry.get("verified_at"),
            verified_run_id=entry.get("verified_run_id"),
            verified_engine=entry.get("verified_engine"),
            products_seen=int(entry.get("products_seen") or 0),
            blocker=entry.get("blocker"),
        )


def record_success(
    key: str,
    *,
    engine: str,
    products: int,
    run_id: str | None = None,
    path: Path | None = None,
) -> None:
    """Record that a retailer produced real products in a real run.

    Called only from a live path. A run that produced nothing must go to
    :func:`record_blocker` instead — "reachable" is not "working".
    """
    retailer = get(key)
    if products <= 0:
        raise ValueError(
            "record_success requires products > 0; use record_blocker for a run that "
            "produced nothing"
        )
    retailer.verification = Verification(
        verified_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        verified_run_id=run_id,
        verified_engine=engine,
        products_seen=products,
        blocker=None,
    )
    _write_ledger(path)


def record_blocker(key: str, reason: str, *, path: Path | None = None) -> None:
    """Record why a retailer could not be used. Does not clear an existing success."""
    retailer = get(key)
    if not retailer.verification.is_verified:
        retailer.verification.blocker = reason
    _write_ledger(path)


def _write_ledger(path: Path | None = None) -> None:
    ledger = path or LEDGER_PATH
    try:
        ledger.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            key: retailer.verification.to_dict()
            for key, retailer in RETAILERS.items()
            if retailer.verification.verified_at or retailer.verification.blocker
        }
        ledger.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        # A ledger we cannot write is a lost convenience, not a failed run. The
        # in-memory verification still governs this process.
        pass


def verified_keys() -> list[str]:
    return [key for key, r in RETAILERS.items() if r.verification.is_verified]


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #


def search(
    retailer: Retailer,
    *,
    items: list[Item],
    pincode: str,
    engine: str,
    settings: Settings,
    verbose: bool = True,
    screenshot_dir: Path | None = None,
    country: str | None = "IN",
    profile_id: str | None = None,
) -> RetailerResult:
    """Search one retailer for the items in the list.

    ``engine`` selects the execution layer. ``auto`` prefers the TinyFish Browser API
    and falls back to the local stealth browser, and whichever ran is recorded on the
    result and on every offer — the brief requires that a fallback is never silent.
    """
    import sys

    queries = [item.search_query for item in items]
    if verbose:
        print(
            f"[adapters] {retailer.name}: {len(queries)} quer{'y' if len(queries) == 1 else 'ies'} "
            f"via {engine}",
            file=sys.stderr,
            flush=True,
        )

    if engine == ENGINE_CLOAK:
        result = _search_cloak(retailer, queries, pincode, verbose, screenshot_dir)
    elif engine == ENGINE_TINYFISH_BROWSER:
        result = _search_tinyfish(
            retailer, queries, pincode, verbose, screenshot_dir, settings, country, profile_id
        )
    elif engine == ENGINE_TINYFISH_AGENT:
        result = _search_agent(retailer, queries, pincode, verbose, settings)
    elif engine == "auto":
        result = _search_tinyfish(
            retailer, queries, pincode, verbose, screenshot_dir, settings, country, profile_id
        )
        if not result.succeeded:
            if verbose:
                print(
                    f"[adapters] TinyFish Browser did not succeed "
                    f"({result.error_code or result.status}) — falling back to the local "
                    "browser, and labelling the data with the engine that produced it",
                    file=sys.stderr,
                    flush=True,
                )
            fallback = _search_cloak(retailer, queries, pincode, verbose, screenshot_dir)
            if fallback.succeeded:
                fallback.raw["primary_engine_failure"] = {
                    "engine": result.engine,
                    "status": result.status,
                    "error_code": result.error_code,
                    "error_message": result.error_message,
                }
                fallback.warnings.insert(
                    0,
                    f"TinyFish Browser failed ({result.error_code or result.status}); "
                    f"data came from {fallback.engine}",
                )
                result = fallback
            else:
                result.raw["fallback_also_failed"] = {
                    "engine": fallback.engine,
                    "error_code": fallback.error_code,
                }
    else:
        raise ValueError(f"unknown engine {engine!r}")

    _update_ledger(retailer, result)
    return result


def _search_tinyfish(
    retailer: Retailer,
    queries: list[str],
    pincode: str,
    verbose: bool,
    screenshot_dir: Path | None,
    settings: Settings,
    country: str | None,
    profile_id: str | None = None,
) -> RetailerResult:
    from .tinyfish_browser import search as tf_search

    return tf_search(
        settings=settings,
        retailer=retailer.name,
        # Always land on the retailer's home page, never straight on a search URL.
        # This is not cosmetic: entering directly on /s/?q=milk makes the search the
        # *first* navigation of a cold session, and that is exactly the navigation
        # Blinkit challenges — a live test with a one-item list timed out on it while
        # the same query succeeded in a three-item run that landed on the home page
        # first. Landing on the home page makes every search a route change from a
        # page that has already settled, which is what fires the XHR reliably.
        retailer_url=retailer.url,
        pincode=pincode,
        queries=queries,
        country=country,
        screenshot_dir=screenshot_dir,
        verbose=verbose,
        profile_id=profile_id,
    )


def _search_cloak(
    retailer: Retailer,
    queries: list[str],
    pincode: str,
    verbose: bool,
    screenshot_dir: Path | None,
) -> RetailerResult:
    from .cloak_client import search as cloak_search

    return cloak_search(
        retailer=retailer.name,
        retailer_url=retailer.url,
        pincode=pincode,
        queries=queries,
        headless=True,
        screenshot_dir=screenshot_dir,
        verbose=verbose,
    )


def _search_agent(
    retailer: Retailer,
    queries: list[str],
    pincode: str,
    verbose: bool,
    settings: Settings,
) -> RetailerResult:
    from .tinyfish_client import TinyFishClient

    with TinyFishClient(settings, verbose=verbose) as client:
        return client.search(
            retailer=retailer.name,
            retailer_url=retailer.url,
            pincode=pincode,
            queries=queries,
            browser_profile="stealth",
            proxy_country=None,
        )


def _update_ledger(retailer: Retailer, result: RetailerResult) -> None:
    """Record the outcome, so the ledger reflects reality rather than intent."""
    if result.succeeded:
        record_success(
            retailer.key,
            engine=result.engine,
            products=len(result.usable_products),
            run_id=result.run_id,
        )
    elif not retailer.verification.is_verified:
        record_blocker(
            retailer.key,
            f"{result.error_code or result.status}: {result.error_message or ''}".strip()[:400],
        )


# --------------------------------------------------------------------------- #
# Offers
# --------------------------------------------------------------------------- #


def offers_from_result(
    result: RetailerResult,
    items: list[Item],
    *,
    retailer_key: str,
    min_confidence: float = 0.0,
) -> list[Offer]:
    """Turn a retailer's products into offers, matched to the items requested.

    A product that matches nothing is dropped rather than attached to the nearest
    item — an unmatched product is not a partial answer, it is a different product.
    """
    by_query: dict[str, list[Item]] = {}
    for item in items:
        by_query.setdefault(item.search_query.lower(), []).append(item)

    offers: list[Offer] = []
    for product in result.products:
        if not product.is_usable:
            continue

        candidates = by_query.get((product.query or "").lower())
        if not candidates:
            # Fall back to trying every item: the query a product came back under is a
            # hint, not a guarantee, since retailers return cross-category results.
            candidates = items

        best: tuple[Any, Item] | None = None
        for item in candidates:
            verdict = match(product, item)
            if not verdict.accepted:
                continue
            if best is None or verdict.confidence > best[0].confidence:
                best = (verdict, item)

        if best is None or best[0].confidence < min_confidence:
            continue

        verdict, item = best
        offer = offer_from_product(
            product,
            retailer_key=retailer_key,
            retailer=result.retailer,
            item_id=item.item_id,
            engine=result.engine,
            source=f"{result.engine}_live",
            match_method=verdict.method,
        )
        # Carry the pack-size verdict across, so the optimizer can prefer a correctly
        # sized pack and ask for approval when only a different size exists.
        offer.pack_mismatch = verdict.pack_mismatch
        offer.pack_note = verdict.pack_note
        offers.append(offer)
    return offers
