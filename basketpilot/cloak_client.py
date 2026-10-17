"""Engine B — the local stealth-browser rescue path.

Runs a CloakBrowser (stealth Chromium, Playwright-compatible) on the local machine.
The point of this engine is the network position: it egresses from a residential
Indian IP, whereas Engine A's remote browser egresses from a datacentre, which is
exactly the profile Cloudflare challenges hardest.

Extraction is a ladder, tried in order, and every rung records where its data came
from so a result can always be traced back to an observation:

  1. A JSON response the site itself fetched, captured from network traffic. We do
     not hardcode an endpoint — we watch what the page actually requests and read
     the real one out of the traffic.
  2. Structured data embedded in the page HTML.
  3. Tolerant DOM extraction, as a last resort.

Rung 1 is preferred because it yields clean structured data with no DOM scraping.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .models import Product, RetailerResult, parse_price_displayed

# --- Selectors and endpoints, as observed on live blinkit.com ------------------
#
# These are not guesses. They were read off a real stealth-browser session
# (tools/probe_location.py, tools/probe_search.py) and are recorded here with the
# evidence, because Blinkit's generated class hashes change and the next person to
# debug this needs to know which part is stable and which is not.

# The location modal AUTO-OPENS on landing when no delivery location is set. There is
# nothing to click first: its overlay intercepts pointer events, so any attempt to
# click a "Select Location" control times out. This is the single most confusing part
# of the flow and it cost two probe iterations to find.
LOCATION_MODAL = "div[class*='LocationModalContainer']"
LOCATION_OVERLAY = "div[class*='LocationOverlay']"
# The modal's own input. The placeholder text is stable; the class hash is not.
LOCATION_INPUT = "input[placeholder='search delivery location']"
LOCATION_INPUT_FALLBACK = "div[class*='LocationSearchBox'] input"

# Blinkit's search results are fetched by its own frontend from this endpoint:
#   POST /v1/layout/search?q=<query>&search_type=type_to_search
# We do NOT replay this call ourselves — it needs a rotating `auth_key` plus
# `session_uuid`, `device_id` and `lat`/`lon` headers. Instead we let the page make the
# call naturally and read the response off the wire. Fewer things to keep in sync, and
# it inherits the Cloudflare-cleared session for free.
SEARCH_ENDPOINT_MARKER = "/v1/layout/search"

# The search page is a plain page load, which is far more reliable than driving the
# home page's search overlay (the home page has no search <input> at all — only the
# location modal does).
SEARCH_URL_TEMPLATE = "https://blinkit.com/s/?q={query}"

# Product cards in the search payload are identified by this widget_type. Everything
# else in `snippets` is a banner, header or rail.
PRODUCT_WIDGET_TYPE = "product_card_snippet_type_2"

# Keys that suggest a dict is a product record inside an arbitrary JSON payload.
NAME_KEYS = ("name", "product_name", "title", "display_name", "productName")
PRICE_KEYS = ("price", "selling_price", "mrp", "offer_price", "discounted_price", "sp")
SIZE_KEYS = ("unit", "pack_size", "quantity", "size", "weight", "unit_size")
BRAND_KEYS = ("brand", "brand_name", "manufacturer")

# Substrings that suggest a JSON blob contains product data. Used to rank captured
# responses, so we pick the real search response rather than a suggestion call.
PRODUCT_HINTS = (
    "price",
    "mrp",
    "product_id",
    "inventory",
    "snippets",
    "brand",
    "unit",
    "selling_price",
)


class CloakBrowserUnavailable(RuntimeError):
    """CloakBrowser could not be launched at all."""


class Blocked(RuntimeError):
    """The site served a bot challenge instead of content."""


def detect_challenge(page: Any, response: Any = None) -> str | None:
    """Return a description if the page is a bot challenge rather than real content.

    This matters more than it looks. A Cloudflare interstitial is a full HTML
    document, so a naive DOM extractor will happily parse it, find zero products,
    and report a clean empty result — a silent block that looks like a successful
    search with no matches. We check the response and the page, not just the DOM.
    """
    if response is not None:
        headers = getattr(response, "headers", None) or {}
        if str(headers.get("cf-mitigated", "")).lower() == "challenge":
            return "Cloudflare managed challenge (cf-mitigated: challenge)"

    try:
        url = page.url or ""
        if "__cf_chl_" in url or "/cdn-cgi/challenge-platform" in url:
            return f"Cloudflare challenge interstitial at {url}"
        html = page.content() or ""
        # NOT "challenge-platform". That string is Cloudflare's Turnstile *script tag*,
        # which Blinkit serves on every page including perfectly healthy ones. It was in
        # this list until a run proved it wrong: the marker was reported present, the
        # very next search on the same page returned 12 products, and the marker never
        # went away. A detector that fires on a working page is worse than no detector —
        # it burned 45s per query waiting for a challenge that did not exist.
        #
        # What remains are markers of an actual interstitial: `cf-chl-` ids only appear
        # in the challenge document, and the two titles are the interstitial's own.
        for marker in ("cf-chl-", "Just a moment...", "Attention Required"):
            if marker in html:
                return f"challenge marker {marker!r} present in the page"
    except Exception:  # noqa: BLE001 - detection must never break the run
        return None

    return None


def _launch(**kwargs: Any) -> Any:
    """Import and launch CloakBrowser, converting setup failures into a clear error."""
    try:
        from cloakbrowser import launch
    except ImportError as exc:  # pragma: no cover - environment problem
        raise CloakBrowserUnavailable(
            "cloakbrowser is not installed. Install it with:\n"
            "\n"
            "    pip install cloakbrowser\n"
            "\n"
            "The first launch downloads a stealth Chromium build (~200MB, cached)."
        ) from exc

    try:
        return launch(**kwargs)
    except Exception as exc:  # noqa: BLE001 - surfaced to the user verbatim
        raise CloakBrowserUnavailable(
            f"CloakBrowser failed to launch: {exc}\n"
            "\n"
            "Common causes on macOS: the ad-hoc-signed binary needs quarantine cleared\n"
            "    xattr -cr ~/.cloakbrowser\n"
            "or Playwright's system dependencies are missing:\n"
            "    playwright install-deps chromium\n"
            "\n"
            "If a license error appears, note that keyless use should fall back to the\n"
            "free v146 binary. See https://cloakbrowser.dev/free"
        ) from exc


def _recorder_emitter(page: Any, log: Any) -> Any:
    """The browser context that owns ``page``, falling back to the page itself.

    The recorder wants the context so its body reads stay out of the page's own event
    dispatch — see :meth:`_NetworkRecorder.attach`. The fallback is defensive: if a
    Playwright build does not expose ``page.context``, page-level capture is still
    better than no capture, so it is used and the reduced safety is logged rather than
    assumed away.
    """
    try:
        context = page.context
        if context is not None:
            return context
    except Exception as exc:  # noqa: BLE001
        log(
            f"could not reach the browser context ({type(exc).__name__}: {exc}); "
            "capturing network at page level instead"
        )
    else:
        log("no browser context available; capturing network at page level instead")
    return page


class _NetworkRecorder:
    """Records the JSON the page fetches, so we can find the site's own API.

    We capture passively during a real UI-driven search rather than guessing an
    endpoint. Whatever the page requests *is* the endpoint — no URL reconstruction,
    no header forging, and nothing to go stale except the site itself.
    """

    def __init__(self) -> None:
        self.responses: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        # Why a response was not captured. Kept so a missing search response can be
        # explained from the run log instead of guessed at.
        self.skipped: list[str] = []

    def attach(self, emitter: Any) -> None:
        """Attach to the browser *context*, not the page — and that distinction matters.

        Bodies must be read the instant a response arrives. The site's SPA navigates
        again moments later and Playwright then refuses to hand over the body at all:
        *"Response body is not available for a response that was navigated away from."*
        So the read happens in the handler, eagerly.

        Reading a body is a blocking Playwright call, which re-enters the sync API. Doing
        that from a *page* handler runs it inside Playwright's own
        ``page.emit(Page.Events.Response, response)`` — the same dispatch that drives
        ``expect_response``'s waiter. The waiter can then fulfil and tear itself down
        while pyee is still iterating the handler list, and pyee removes listeners with a
        naked ``list.pop``, so the second cleanup raises::

            KeyError: <function Waiter.reject_on_event.<locals>.listener ...>

        Playwright emits a response to the context *first* and the page *second*, as two
        sequential emits rather than one nested pair (``_browser_context._on_response``).
        Attaching here therefore keeps the body read out of the page's dispatch while
        still seeing every response — eager capture, no re-entrancy.
        """
        emitter.on("response", self._on_response)
        emitter.on("request", self._on_request)

    def _on_request(self, request: Any) -> None:
        """Record XHR/fetch requests, including headers needed to replay them.

        Blinkit's internal endpoints have historically required an auth or device
        header, so a bare re-fetch of the URL would fail even from a page that has
        already cleared the challenge. We keep the headers so a replay can reuse them.
        """
        try:
            if request.resource_type not in ("xhr", "fetch"):
                return
            self.requests.append(
                {
                    "url": request.url,
                    "method": request.method,
                    "headers": dict(request.headers),
                    "post_data": request.post_data,
                    "resource_type": request.resource_type,
                }
            )
        except Exception:  # noqa: BLE001
            return

    def _on_response(self, response: Any) -> None:
        # Read the body here and now, not later: see ``attach`` for why this handler is
        # on the context rather than the page, and why the read cannot wait.
        url = ""
        try:
            request = response.request
            url = response.url
            if request.resource_type not in ("xhr", "fetch", "document"):
                return
            content_type = (response.headers or {}).get("content-type", "")
            if "json" not in content_type.lower():
                self.skipped.append(f"{url[:110]} | content-type={content_type[:40]!r}")
                return
            if response.status >= 400:
                self.skipped.append(f"{url[:110]} | http {response.status}")
                return
            body = response.json()
        except Exception as exc:  # noqa: BLE001 - a failed capture must never break the run
            # Most often this means the body was already discarded because the page
            # navigated away — which is exactly what happens when the site reloads
            # itself after the delivery location changes.
            self.skipped.append(f"{url[:110]} | body unreadable: {type(exc).__name__}: {exc}")
            return
        self.responses.append(
            {
                "url": response.url,
                "status": response.status,
                "content_type": content_type,
                "body": body,
                "request": {
                    "url": request.url,
                    "method": request.method,
                    "headers": dict(request.headers),
                    "post_data": request.post_data,
                },
            }
        )

    def candidates_for(self, query: str) -> list[dict[str, Any]]:
        """Rank every capture in the session by product-likeness.

        Retained for ad-hoc probing. Note this is session-wide and therefore NOT safe
        for attributing products to a query — see ``_rank_by_product_likeness`` for the
        scoped version the search flow uses.
        """
        return _rank_by_product_likeness(self.responses, query)


def _walk_for_products(node: Any, depth: int = 0) -> list[dict[str, Any]]:
    """Find product-shaped dicts anywhere inside an arbitrary JSON structure.

    A dict counts as product-shaped if it has a name-like key and at least one of a
    price-like or size-like key. This is deliberately shape-based rather than
    endpoint-specific, because we are reading an undocumented internal payload.
    """
    if depth > 12:
        return []

    found: list[dict[str, Any]] = []

    if isinstance(node, dict):
        keys = {k.lower() for k in node.keys()}
        has_name = any(k in keys for k in NAME_KEYS)
        has_price = any(k in keys for k in PRICE_KEYS)
        has_size = any(k in keys for k in SIZE_KEYS)
        if has_name and (has_price or has_size):
            found.append(node)
        for value in node.values():
            found.extend(_walk_for_products(value, depth + 1))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk_for_products(item, depth + 1))

    return found


def _pick(record: dict[str, Any], candidates: tuple[str, ...]) -> Any:
    """Return the first present, non-empty value among case-insensitive candidate keys."""
    lowered = {k.lower(): v for k, v in record.items()}
    for key in candidates:
        value = lowered.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _product_from_record(record: dict[str, Any], query: str) -> Product | None:
    """Map one product-shaped JSON record onto our Product model."""
    name = _pick(record, NAME_KEYS)
    if not name or not isinstance(name, (str, int, float)):
        return None

    price_raw = _pick(record, PRICE_KEYS)
    price_displayed = None
    if price_raw is not None:
        # Some payloads carry a numeric paise/rupee value rather than a string.
        if isinstance(price_raw, (int, float)):
            price_displayed = f"₹{price_raw}"
        else:
            price_displayed = str(price_raw)

    size = _pick(record, SIZE_KEYS)
    brand = _pick(record, BRAND_KEYS)

    product = Product(
        query=query,
        name=str(name),
        availability="unknown",
        brand=str(brand) if brand else None,
        package_size=str(size) if size else None,
        price_displayed=price_displayed,
        currency="INR" if price_displayed else None,
        product_url=None,
    )
    product.validate()
    return product if product.is_valid else None


def _url_names_query(url: str, query: str) -> bool:
    """True if a search URL is for this specific query.

    Blinkit's search endpoint carries the query in the URL — either as `q=<query>` on
    the first page or `actual_query=<query>` on pagination. Checking this is what makes
    attribution trustworthy: responses arrive asynchronously and a slow one from the
    previous query can land while the next query is running, so slicing captures by
    arrival index is not enough to keep them apart.
    """
    token = quote(query.strip().lower())
    lowered = url.lower()
    return f"q={token}" in lowered or f"actual_query={token}" in lowered


def _extract_from_network(entries: list[dict[str, Any]], query: str) -> list[Product]:
    """Rung 1 — read the site's own JSON responses, captured passively during search.

    Every entry is checked against the query in its URL before its products are
    believed. Without that check an earlier version labelled bread results as eggs:
    the payload for one query mentions other search terms (the response for "milk"
    contains "bread" inside category names), so a content-ranked scan matches the
    wrong response and silently attributes products to the wrong query.

    Preferred path: Blinkit's own search endpoint, parsed by its exact known shape.
    Fallback: the generic shape-based walk, in case the endpoint or schema changes.
    """
    products: list[Product] = []
    seen: set[str] = set()

    def add(product: Product | None, entry: dict[str, Any]) -> None:
        if product is None or product.name in seen:
            return
        seen.add(product.name)
        product.source = "cloak_json"
        product.warnings.append(f"source: {entry['url'][:160]}")
        products.append(product)

    # Exact-shape parse first, over only the responses that name this query.
    for entry in entries:
        url = entry.get("url", "")
        if SEARCH_ENDPOINT_MARKER in url and _url_names_query(url, query):
            for product in _products_from_search_payload(entry.get("body"), query):
                add(product, entry)
    if products:
        return products

    # Nothing matched the known shape — fall back to the generic walk.
    #
    # The same attribution rule applies here, and it matters more: this path scans by
    # content, so another query's response is exactly what it would otherwise latch
    # onto. A search response that names a different query is skipped outright rather
    # than mined for products. Responses from unrelated endpoints are still allowed,
    # since those carry no query to check against.
    for entry in _rank_by_product_likeness(entries, query):
        url = entry.get("url", "")
        if SEARCH_ENDPOINT_MARKER in url and not _url_names_query(url, query):
            continue
        for record in _walk_for_products(entry["body"]):
            add(_product_from_record(record, query), entry)

    return products


def _rank_by_product_likeness(
    entries: list[dict[str, Any]], query: str
) -> list[dict[str, Any]]:
    """Order captures by how product-like their contents are, best first."""
    token = re.sub(r"\s+", "", query).lower()
    scored: list[tuple[int, dict[str, Any]]] = []
    for entry in entries:
        blob = json.dumps(entry.get("body"), default=str).lower()
        score = sum(hint in blob for hint in PRODUCT_HINTS)
        if token and token in blob:
            score += 2
        if "search" in entry.get("url", "").lower():
            score += 1
        if score > 0:
            scored.append((score, entry))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [entry for _, entry in scored]


def _search_and_collect(
    page: Any, recorder: _NetworkRecorder, query: str, log: Any
) -> list[dict[str, Any]]:
    """Search for one query and return the JSON captured during that search.

    Waiting is done with Playwright's own ``expect_response`` rather than a polling
    loop around ``time.sleep``. This is not a style choice — it is the difference
    between working and not working.

    In the sync API, response events are dispatched only while the main thread is
    *inside* a Playwright call. A loop that checks a list and calls ``time.sleep``
    never enters one, so queued response events are never dispatched and every
    capture appears to be missing. ``expect_response`` both pumps the event loop and
    waits for exactly the response we want.
    """
    url = SEARCH_URL_TEMPLATE.format(query=quote(query))
    mark = len(recorder.responses)

    try:
        with page.expect_response(
            lambda response: SEARCH_ENDPOINT_MARKER in response.url, timeout=35_000
        ) as info:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            # Read the body HERE, inside the block and before anything else. Blinkit's
            # SPA navigates again moments after the search XHR, and a body that is not
            # read before that navigation is gone for good:
            #   "Response body is not available for a response that was navigated away
            #    from. Read response.body() before triggering any navigation."
            response = info.value
            try:
                body = response.json()
            except Exception as exc:  # noqa: BLE001
                log(f"{query!r}: search body unreadable: {type(exc).__name__}: {exc}")
                body = None
        log(f"searched {query!r} — search API responded ({response.status})")
        # The recorder usually captured this same response a moment earlier, from the
        # context's emit, which precedes the page's. Only add it here when the recorder
        # did not get it — otherwise every search payload is recorded twice, which
        # double-counts the capture log and hands extraction the same body twice.
        already = any(
            entry["url"] == response.url and entry["status"] == response.status
            for entry in recorder.responses[mark:]
        )
        if body is not None and not already:
            log(f"{query!r}: the recorder missed this response; keeping the copy read here")
            recorder.responses.append(
                {
                    "url": response.url,
                    "status": response.status,
                    "content_type": "application/json",
                    "body": body,
                    "request": {"url": response.url, "method": "POST", "headers": {},
                                "post_data": None},
                }
            )
    except Exception as exc:  # noqa: BLE001
        log(f"{query!r}: no search API response observed ({type(exc).__name__})")
        for reason in recorder.skipped[-6:]:
            log(f"    not captured: {reason}")

    # Let the grid render, which the DOM fallback needs.
    time.sleep(8.0)
    log(f"{query!r}: landed on {page.url}")
    return recorder.responses[mark:]


def _text_of(node: Any) -> str | None:
    """Pull the display string out of Blinkit's `{text: ..., color: ..., font: ...}`.

    Nearly every visible string in the layout payload is wrapped in this envelope, so
    the real text is always one level down under `text`.
    """
    if isinstance(node, str):
        return node.strip() or None
    if isinstance(node, dict):
        inner = node.get("text")
        if isinstance(inner, str):
            return inner.strip() or None
    return None


def _products_from_search_payload(body: Any, query: str) -> list[Product]:
    """Parse Blinkit's `/v1/layout/search` response into validated products.

    The observed shape is::

        {"is_success": true,
         "response": {"snippets": [
             {"widget_type": "product_card_snippet_type_2",
              "data": {...product...}}, ...]}}

    Anti-fabrication: the displayed price string (`normal_price.text`, e.g. "₹49") is
    the source of truth. The payload *also* carries a numeric price inside
    `atc_action.add_to_cart.cart_item.price`, which we use only as corroboration — if
    the two disagree we keep the displayed string and record a warning rather than
    silently preferring one.
    """
    if not isinstance(body, dict) or not body.get("is_success"):
        return []

    response = body.get("response")
    if not isinstance(response, dict):
        return []

    snippets = response.get("snippets")
    if not isinstance(snippets, list):
        return []

    products: list[Product] = []
    for snippet in snippets:
        if not isinstance(snippet, dict):
            continue
        if snippet.get("widget_type") != PRODUCT_WIDGET_TYPE:
            continue
        data = snippet.get("data")
        if not isinstance(data, dict):
            continue
        product = _product_from_card(data, query)
        if product is not None:
            products.append(product)
    return products


def _product_from_card(card: dict[str, Any], query: str) -> Product | None:
    """Convert one `product_card_snippet_type_2` card into a Product."""
    name = _text_of(card.get("name"))
    if not name:
        return None

    price_displayed = _text_of(card.get("normal_price"))
    mrp_displayed = _text_of(card.get("mrp"))
    package_size = _text_of(card.get("variant"))

    # The brand is a rendered card field (`data.brand_name`, same {text,color,font}
    # envelope as the name), so it is read off the card rather than inferred from the
    # product name — inferring would mean guessing where a name stops being a brand.
    brand = _text_of(card.get("brand_name"))

    # `inventory` is a real integer count, not a guess: >0 means it can be added.
    inventory = card.get("inventory")
    if isinstance(inventory, bool) or not isinstance(inventory, (int, float)):
        availability = "unknown"
    else:
        availability = "in_stock" if inventory > 0 else "out_of_stock"

    product_id = None
    identity = card.get("identity")
    if isinstance(identity, dict):
        raw_id = identity.get("id")
        if raw_id is not None:
            product_id = str(raw_id)

    # Corroborate the displayed price against the numeric one Blinkit attaches to the
    # add-to-cart payload. Both come from the same response, so a mismatch means we
    # misread one of them and the caller should know.
    warnings: list[str] = []
    numeric_from_card = _numeric_price_from_card(card)
    displayed_numeric = parse_price_displayed(price_displayed) if price_displayed else None
    if (
        numeric_from_card is not None
        and displayed_numeric is not None
        and abs(numeric_from_card - displayed_numeric) > 0.01
    ):
        warnings.append(
            f"price mismatch: displayed {price_displayed!r} vs payload {numeric_from_card}; "
            "kept the displayed value"
        )

    # Cross-check the rendered brand against the one Blinkit puts in the add-to-cart
    # payload. A disagreement means we read the wrong node; say so rather than pick one.
    brand_from_payload = _brand_from_card(card)
    if brand and brand_from_payload and brand.lower() != brand_from_payload.lower():
        warnings.append(
            f"brand mismatch: card shows {brand!r} but payload says {brand_from_payload!r}; "
            "kept the rendered value"
        )

    evidence_parts = [
        part for part in (name, brand, package_size, price_displayed, mrp_displayed) if part
    ]
    product = Product(
        query=query,
        name=name,
        availability=availability,
        brand=brand,
        package_size=package_size,
        price_displayed=price_displayed,
        currency="INR" if price_displayed and "₹" in price_displayed else None,
        product_url=_product_url(product_id, name),
        evidence=" | ".join(evidence_parts) or None,
    )
    if product_id:
        product.warnings.append(f"blinkit product_id={product_id}")
    if isinstance(inventory, (int, float)) and not isinstance(inventory, bool):
        product.warnings.append(f"inventory={inventory:g}")
    product.warnings.extend(warnings)
    # Run the shared validation gate. Without this the numeric price is never derived
    # from the displayed string, and the corroboration check never runs.
    product.validate()
    return product


def _brand_from_card(card: dict[str, Any]) -> str | None:
    """Read the brand Blinkit attaches to the card's add-to-cart payload.

    Used only to corroborate the rendered `brand_name` field, never as the primary
    source — the rendered value is what a human would see on the card.
    """
    atc = card.get("atc_action")
    if not isinstance(atc, dict):
        return None
    add = atc.get("add_to_cart")
    if not isinstance(add, dict):
        return None
    item = add.get("cart_item")
    if not isinstance(item, dict):
        return None
    brand = item.get("brand")
    return str(brand) if brand else None


def _numeric_price_from_card(card: dict[str, Any]) -> float | None:
    """Read the numeric price Blinkit attaches to the card's add-to-cart payload."""
    atc = card.get("atc_action")
    if not isinstance(atc, dict):
        return None
    add = atc.get("add_to_cart")
    if not isinstance(add, dict):
        return None
    item = add.get("cart_item")
    if not isinstance(item, dict):
        return None
    price = item.get("price")
    if isinstance(price, bool) or not isinstance(price, (int, float)):
        return None
    return float(price)


def _product_url(product_id: str | None, name: str) -> str | None:
    """Build the canonical web product URL.

    The payload's own `click_action.blinkit_deeplink.url` is an app deeplink
    (`grofers://pdp?product_id=...`), which is useless in a report. Blinkit's web
    product pages are `/prn/<slug>/prid/<id>`, and the slug is cosmetic — the id is
    the real key. Verified against the live site (tools/probe_url.py, 3/3 cases):
    each synthesised URL returned HTTP 200, the page showed the product name, and the
    ADD button was present.
    """
    if not product_id:
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "product").lower()).strip("-") or "product"
    return f"https://blinkit.com/prn/{slug}/prid/{product_id}"


def _extract_from_dom(page: Any, query: str) -> list[Product]:
    """Rung 3 — read the rendered product cards as text.

    Last resort, used only if the search API yields nothing. Deliberately *not*
    anchored on `<a href>`: on a live Blinkit search page there are no product
    anchors at all — cards are divs with click handlers — so a link-based selector
    silently matches nothing (tools/probe_url.py observed 0 product anchors on
    /s/?q=eggs).

    Instead we take the text of the rendered grid and split it into cards, using the
    "ADD" button that every in-stock card carries as the delimiter. Everything here is
    read off the screen; nothing is inferred.
    """
    products: list[Product] = []
    try:
        blocks = page.eval_on_selector_all(
            "div",
            """els => els
                .filter(e => {
                    const t = e.innerText || '';
                    return t.includes('ADD') && /₹/.test(t) && t.length < 400;
                })
                .map(e => (e.innerText || '').trim())""",
        )
    except Exception:  # noqa: BLE001
        return products

    seen: set[str] = set()
    for text in blocks or []:
        lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
        price_line = next((ln for ln in lines if parse_price_displayed(ln) is not None), None)
        if not price_line:
            continue
        # The name is the longest line that is not a price, a size or the button.
        candidates = [
            ln
            for ln in lines
            if parse_price_displayed(ln) is None and ln.upper() != "ADD" and not ln.isdigit()
        ]
        if not candidates:
            continue
        name = max(candidates, key=len)
        if name in seen:
            continue
        seen.add(name)

        size_line = next(
            (ln for ln in lines if re.fullmatch(r"\d+(\.\d+)?\s*(ml|l|g|kg|pcs?|pack)", ln, re.I)),
            None,
        )
        product = Product(
            query=query,
            name=name,
            availability="in_stock" if "ADD" in text.upper() else "unknown",
            package_size=size_line,
            price_displayed=price_line,
            currency="INR" if "₹" in price_line else None,
            evidence=text[:300],
            source="cloak_dom",
        )
        product.validate()
        if product.is_valid:
            product.warnings.append("source: dom text extraction (search API unavailable)")
            # Blinkit's search cards carry no product id in any attribute or href —
            # verified with tools/probe_dom_id.py, which found none — so a row read
            # from the grid cannot be given a product URL. Recorded rather than left
            # as a silent gap, so a consumer knows why the field is null.
            product.warnings.append("no product_url: the search page exposes no product ids")
            products.append(product)

    return products


def _try_selectors(page: Any, selectors: list[str]) -> Any:
    """Return the first locator that resolves to a visible element, else None."""
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() > 0 and locator.is_visible():
                return locator
        except Exception:  # noqa: BLE001
            continue
    return None


def _set_delivery_location(page: Any, pincode: str, log: Any) -> bool:
    """Set the delivery location, if the site is asking for one.

    Two behaviours have to be handled, and getting this wrong is what made an earlier
    version of this function hang for 30 seconds:

      * **No location set** — the modal auto-opens on landing. Its overlay swallows
        clicks, so the only thing to do is type into the modal's own input.
      * **Location already known** — the modal never appears. Blinkit remembers the
        location against the session/device, so on a second run there is nothing to do.
        Waiting for a modal that will never come is a guaranteed timeout.

    So we look for the modal with a short timeout and treat "not there" as success,
    not failure. The real check is whether the catalogue is usable afterwards, which
    the caller verifies.

    Returns True if a location is set (by us or already), False if the modal was
    present but we could not drive it.
    """
    try:
        page.wait_for_selector(LOCATION_INPUT, state="visible", timeout=8000)
    except Exception:  # noqa: BLE001
        log("no location modal — a delivery location is already set for this session")
        return True

    locator = _try_selectors(page, [LOCATION_INPUT, LOCATION_INPUT_FALLBACK])
    if locator is None:
        log("location modal appeared but its input could not be located")
        return False

    try:
        locator.click()
        time.sleep(0.4)
        # type() rather than fill(): keystroke-level input is what the site expects and
        # is less likely to be flagged than an instantaneous value injection.
        locator.type(pincode, delay=150)
        time.sleep(5.0)  # suggestions are debounced; wait for them to arrive

        # Clicking the row that is exactly the pincode is what actually commits the
        # location. Pressing Enter alone did not take, in testing.
        committed = False
        try:
            page.get_by_text(pincode, exact=True).first.click(timeout=6000)
            committed = True
        except Exception:  # noqa: BLE001
            log(f"exact {pincode!r} suggestion not clickable; pressing Enter instead")
            page.keyboard.press("Enter")

        try:
            page.wait_for_selector(LOCATION_OVERLAY, state="detached", timeout=25_000)
            log(f"delivery location set to {pincode} (suggestion clicked: {committed})")
        except Exception:  # noqa: BLE001
            log("location modal did not dismiss within 25s")
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        log(f"failed to set delivery location: {exc}")
        return False


def _catalogue_is_visible(page: Any) -> bool:
    """True if the site is showing a real catalogue rather than still asking for a location."""
    try:
        body = page.inner_text("body").lower()
    except Exception:  # noqa: BLE001
        return False
    if "provide your delivery location" in body:
        return False
    return "my cart" in body or "delivery in" in body


def search(
    *,
    retailer: str,
    retailer_url: str,
    pincode: str,
    queries: list[str],
    headless: bool = False,
    humanize: bool = True,
    screenshot_dir: Path | None = None,
    storage_state: Path | None = None,
    save_storage_state: Path | None = None,
    verbose: bool = True,
) -> RetailerResult:
    """Drive a local stealth browser through the retailer and extract products."""
    def log(message: str) -> None:
        if verbose:
            # stderr, not stdout: stdout carries the JSON payload and nothing else, so
            # that `--json-only` can be piped straight into a parser.
            print(f"  [cloak] {message}", file=sys.stderr, flush=True)

    result = RetailerResult(
        retailer=retailer,
        engine="cloak",
        queries=queries,
        status="PENDING",
        delivery_pincode=pincode,
    )

    try:
        browser = _launch(
            headless=headless,
            humanize=humanize,
            timezone="Asia/Kolkata",
            locale="en-IN",
        )
    except CloakBrowserUnavailable as exc:
        result.status = "FAILED"
        result.error_code = "ENGINE_UNAVAILABLE"
        result.error_message = str(exc)
        return result

    context = None
    try:
        # A warmed session file lets us skip both the challenge and the location gate.
        # We prefer an explicit state file over a persistent context: a state file is
        # inspectable and testable, and does not leave lock files behind on a crash.
        if storage_state and Path(storage_state).exists():
            log(f"loading saved session state from {storage_state}")
            context = browser.new_context(storage_state=str(storage_state))
            page = context.new_page()
        else:
            page = browser.new_page()

        # The drive loop itself lives in retailer_runner: it is page-level work and
        # is shared verbatim with the TinyFish Browser engine, which reaches the same
        # page over CDP. Imported here rather than at module scope to keep the module
        # graph acyclic — retailer_runner imports this module's extraction ladder.
        from .retailer_runner import drive_page, save_state

        drive_page(
            page,
            result=result,
            retailer_url=retailer_url,
            pincode=pincode,
            queries=queries,
            engine="cloak",
            screenshot_dir=screenshot_dir,
            context=context,
            log=log,
        )

        if save_storage_state:
            save_state(page, context, Path(save_storage_state), log)

    except Blocked as exc:
        result.status = "FAILED"
        result.error_code = "SITE_BLOCKED"
        result.error_message = (
            f"The site served a bot challenge that the local browser did not clear: {exc}. "
            "Consider warming a session by hand with --save-state, or running headed "
            "with --headed so the challenge can be solved interactively."
        )
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        result.status = "FAILED"
        result.error_code = "BROWSER_ERROR"
        result.error_message = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            browser.close()
        except Exception:  # noqa: BLE001
            pass

    return result


def _safe_title(page: Any) -> str:
    try:
        return page.title()
    except Exception:  # noqa: BLE001
        return ""


def _screenshot(page: Any, directory: Path | None, retailer: str, result: RetailerResult, log: Any) -> None:
    """Capture a screenshot for diagnosis. Never raises."""
    if directory is None:
        return
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{retailer.lower().replace(' ', '-')}-cloak-{result.status.lower()}.png"
        page.screenshot(path=str(path))
        result.raw["screenshot"] = str(path)
        log(f"screenshot written to {path}")
    except Exception as exc:  # noqa: BLE001
        log(f"screenshot failed: {exc}")


def _screenshot_named(
    page: Any, directory: Path | None, label: str, log: Any
) -> Path | None:
    """Capture a screenshot under an explicit label. Never raises.

    ``_screenshot`` names its file after ``result.status``, which is right for the
    single end-of-run capture but useless for per-query diagnostics — every empty
    query would overwrite the same file. This variant takes the label directly and
    returns the path so the caller can record it.
    """
    if directory is None:
        return None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^a-z0-9._-]+", "-", label.lower()).strip("-")
        path = directory / f"{safe}.png"
        page.screenshot(path=str(path))
        log(f"screenshot written to {path}")
        return path
    except Exception as exc:  # noqa: BLE001
        log(f"screenshot failed: {exc}")
        return None


def dump_json(obj: Any, path: Path) -> None:
    """Write a JSON artefact, used for captured network payloads during diagnosis."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
