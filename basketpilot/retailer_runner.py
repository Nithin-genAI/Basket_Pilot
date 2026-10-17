"""The engine-agnostic drive loop: given a live page, extract validated products.

This module exists because of what Phase 2 reconnaissance proved. The TinyFish
Browser API hands back a ``cdp_url`` for plain Playwright, and once a page is in
hand, *nothing about the extraction depends on which browser produced it*. The
location gate, the challenge check, the two-attempt JSON ladder, the DOM rung, the
empty-query screenshots — all of it is page-level work.

So it lives here, once. :mod:`basketpilot.cloak_client` and
:mod:`basketpilot.tinyfish_browser` are now thin engines that differ only in how
they obtain a page and how they tear it down. Before this split the choice was to
duplicate ~140 lines of hard-won extraction logic or let the two engines drift;
neither is acceptable when the logic encodes findings like "the JSON rung must be
tried twice because Blinkit's SPA sometimes loses the body to a re-navigation".

The import direction is deliberately one-way — this module imports the ladder from
``cloak_client``, and ``cloak_client`` imports :func:`drive_page` lazily inside its
own ``search()``. That keeps the module graph acyclic while leaving the ladder
where the Phase 1 probes and ``tools/verify.py`` already expect to find it.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from .cloak_client import (
    Blocked,
    _NetworkRecorder,
    _catalogue_is_visible,
    _extract_from_dom,
    _extract_from_network,
    _recorder_emitter,
    _safe_title,
    _screenshot,
    _screenshot_named,
    _search_and_collect,
    _set_delivery_location,
    detect_challenge,
)
from .models import Product, RetailerResult

# Tuned timeouts for fast response and responsive user experience
CHALLENGE_SETTLE_SECONDS = 2.0
CHALLENGE_RETRY_SECONDS = 3.0
CHALLENGE_CLEAR_TIMEOUT = 12.0
CHALLENGE_POLL_SECONDS = 2.0

# Attempts per query before the DOM rung is used.
SEARCH_ATTEMPTS = 2


def wait_for_challenge_to_clear(
    page: Any,
    log: Callable[[str], None],
    timeout: float = CHALLENGE_CLEAR_TIMEOUT,
) -> bool:
    """Poll until the page is free of a bot challenge. Returns True when it is clear.

    Polling is done with ``page.wait_for_timeout``, not ``time.sleep``: in the sync
    API, response events are only dispatched while the main thread is inside a
    Playwright call, so a plain sleep would let the very events we are waiting on sit
    queued and undispatched.
    """
    deadline = time.monotonic() + timeout
    waited = 0.0
    announced = False
    while time.monotonic() < deadline:
        marker = detect_challenge(page)
        if not marker:
            if waited:
                log(f"challenge cleared after {waited:.0f}s")
            return True
        if not announced:
            log(
                f"a bot challenge is on the page ({marker}); waiting up to "
                f"{timeout:.0f}s for it to clear"
            )
            announced = True
        page.wait_for_timeout(int(CHALLENGE_POLL_SECONDS * 1000))
        waited += CHALLENGE_POLL_SECONDS
    log(f"the challenge did not clear within {timeout:.0f}s")
    return False

# Coordinates for the delivery pincodes we expect to be asked about. Several
# quick-commerce sites offer "use my current location" as an alternative to typing a
# pincode, and a browser that has never been granted geolocation silently fails that
# path — the button does nothing and the run looks like a broken selector.
#
# These are pincode centroids, not a user's position: we are answering "which
# delivery zone is this order for", which is the only question being asked. The map
# is deliberately small and explicit; an unknown pincode gets no coordinates rather
# than a guessed location, because a wrong zone returns a real catalogue for the
# wrong place — the exact silent substitution this project refuses to make.
PINCODE_COORDS: dict[str, tuple[float, float]] = {
    "560001": (12.9716, 77.5946),  # Bengaluru, MG Road
    "400001": (19.0760, 72.8777),  # Mumbai, Fort
    "110001": (28.6139, 77.2090),  # New Delhi, Connaught Place
    "600001": (13.0827, 80.2707),  # Chennai
    "500001": (17.3850, 78.4867),  # Hyderabad
    "700001": (22.5726, 88.3639),  # Kolkata
    "411001": (18.5204, 73.8567),  # Pune
    "302001": (26.9124, 75.7873),  # Jaipur
}


def apply_geolocation(context: Any, pincode: str, log: Callable[[str], None]) -> bool:
    """Grant geolocation and place the browser in the delivery pincode's zone.

    Returns True when a location was applied. An unknown pincode is left alone and
    reported, so a site that falls back to geolocation fails loudly rather than
    quietly serving another city's catalogue.
    """
    if context is None:
        return False
    coords = PINCODE_COORDS.get(pincode.strip())
    if coords is None:
        log(
            f"no known coordinates for pincode {pincode!r}; leaving geolocation "
            "ungranted, so a site that asks for it will not silently serve a "
            "different delivery zone"
        )
        return False
    try:
        context.grant_permissions(["geolocation"])
        context.set_geolocation({"latitude": coords[0], "longitude": coords[1]})
        log(f"geolocation granted for pincode {pincode} ({coords[0]:.4f}, {coords[1]:.4f})")
        return True
    except Exception as exc:  # noqa: BLE001 - a missing permission must not end the run
        log(f"could not grant geolocation ({type(exc).__name__}: {exc}); continuing")
        return False


def drive_page(
    page: Any,
    *,
    result: RetailerResult,
    retailer_url: str,
    pincode: str,
    queries: list[str],
    engine: str,
    screenshot_dir: Path | None = None,
    context: Any = None,
    log: Callable[[str], None],
) -> None:
    """Run the full extraction ladder against ``page``, mutating ``result``.

    The page must already be open; this function navigates it. ``context`` is used
    only to persist session state when the caller asks for it, and may be ``None``.

    Raises :class:`~basketpilot.cloak_client.Blocked` when the site serves a bot
    challenge the browser does not clear. Every other failure is recorded on
    ``result`` rather than raised, so a caller always gets a diagnosis back.
    """
    recorder = _NetworkRecorder()
    recorder.attach(_recorder_emitter(page, log))

    # Before navigation, so a site that asks for the location on load gets an answer.
    apply_geolocation(context if context is not None else getattr(page, "context", None), pincode, log)

    log(f"navigating to {retailer_url}")
    response = page.goto(retailer_url, wait_until="domcontentloaded", timeout=60_000)
    time.sleep(CHALLENGE_SETTLE_SECONDS)  # let any bot challenge resolve

    challenge = detect_challenge(page, response)
    if challenge:
        # A managed challenge often clears itself in a real browser given a moment,
        # so give it one honest chance before calling it a block.
        log(f"possible challenge detected ({challenge}); waiting for it to clear")
        time.sleep(CHALLENGE_RETRY_SECONDS)
        challenge = detect_challenge(page)
        if challenge:
            result.raw["landed_url"] = page.url
            result.raw["page_title"] = _safe_title(page)
            _screenshot(page, screenshot_dir, result.retailer, result, log)
            raise Blocked(challenge)
        log("challenge cleared on its own")

    result.raw["landed_url"] = page.url
    result.raw["page_title"] = _safe_title(page)
    log(f"landed on {page.url!r} title={result.raw['page_title']!r}")

    location_ok = _set_delivery_location(page, pincode, log)
    if not _catalogue_is_visible(page):
        # Not fatal on its own — a search may still work — but it is the single
        # most likely reason for an empty result, so say so now rather than
        # letting it surface as a vague "no products extracted" later.
        result.warnings.append(
            f"the catalogue did not look available after setting location {pincode} "
            f"(location step reported {'ok' if location_ok else 'failed'}); "
            "search results may be empty or geo-restricted"
        )
        log("warning: catalogue not visibly available after the location step")

    # What the landing page and the location step fetched. The location payloads are
    # the most useful thing here: when the location step fails, they are the
    # difference between "the site does not serve this pincode" and "the agent could
    # not find the box".
    log(f"{len(recorder.responses)} JSON response(s) captured from the landing and location steps")

    all_products: list[Product] = []
    for query in queries:
        # The JSON rung gets two chances before the DOM rung is used at all. The
        # order matters: the search XHR's body is occasionally lost to a race when
        # Blinkit's SPA navigates again immediately after the response, and a single
        # unlucky attempt is not evidence that the API is unavailable. A fresh
        # search almost always captures cleanly — and the JSON rung is the only rung
        # that yields product ids, and therefore product URLs. Falling back to the
        # grid after one failed capture would silently halve URL coverage.
        from urllib.parse import quote

        if "zepto" in retailer_url.lower():
            target_search_url = f"https://www.zepto.com/search?query={quote(query)}"
        elif "instamart" in retailer_url.lower():
            target_search_url = f"https://www.swiggy.com/instamart/search?query={quote(query)}"
        elif "jiomart" in retailer_url.lower():
            target_search_url = f"https://www.jiomart.com/search/{quote(query)}"
        else:
            target_search_url = f"https://blinkit.com/s/?q={quote(query)}"

        products: list[Product] = []
        for attempt in range(1, SEARCH_ATTEMPTS + 1):
            fresh = _search_and_collect(page, recorder, query, log, search_url=target_search_url)
            log(f"{query!r}: {len(fresh)} JSON response(s) captured during this search")

            products = _extract_from_network(fresh, query)
            if products:
                log(f"{query!r}: {len(products)} product(s) from captured JSON")
                break

            if attempt < SEARCH_ATTEMPTS:
                log(
                    f"{query!r}: no JSON products — re-warming the page, then "
                    f"re-running the search (attempt {attempt + 1}/{SEARCH_ATTEMPTS})"
                )
                # A challenge that fired on this navigation has to be waited out before
                # the next attempt, or the retry walks straight into the same wall.
                wait_for_challenge_to_clear(page, log)
                # Then go back to the retailer's home page. This is not superstition:
                # Blinkit's search is a client-side route, so navigating to a search URL
                # the SPA already believes it is on does not re-issue the search XHR at
                # all — the run then waits 35s for a request that was never made, which
                # is exactly how the first query of a run came back empty while the rest
                # succeeded. Landing on the home page first makes the next search a real
                # route change, so the XHR fires. It also recovers a page left on
                # chrome-error:// by a failed navigation, which `goto` cannot search from.
                try:
                    page.goto(retailer_url, wait_until="domcontentloaded", timeout=20_000)
                    time.sleep(1.0)
                except Exception as exc:  # noqa: BLE001 - a failed re-warm is not fatal
                    log(f"{query!r}: could not re-warm the page ({type(exc).__name__}); continuing")

        if not products:
            # Both JSON attempts came up empty, so read the rendered grid instead.
            # These rows have no product URL — the search page exposes no product
            # ids — but a row with a real price is worth more than no row.
            log(f"{query!r}: JSON rung empty twice, falling back to DOM text extraction")
            products = _extract_from_dom(page, query)
            if products:
                log(f"{query!r}: {len(products)} product(s) from DOM (no product URLs)")

        if not products:
            # Record what the page actually looked like, so a genuine empty result
            # can be told apart from a page that failed to render.
            log(f"{query!r}: nothing extracted after two search attempts")
            result.warnings.append(f"{query!r}: no products after two search attempts")
            shot_path = _screenshot_named(
                page, screenshot_dir, f"{result.retailer}-{query}-empty", log
            )
            if shot_path:
                result.raw.setdefault("empty_query_screenshots", []).append(str(shot_path))

        all_products.extend(products)

    result.products = all_products
    result.raw["captured_json_urls"] = [e["url"] for e in recorder.responses][:60]
    # Which engine produced these rows. Recorded on the result itself so a consumer
    # never has to infer it from the caller's command line.
    result.engine = engine

    if screenshot_dir is not None:
        try:
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            shot = screenshot_dir / f"{result.retailer}-{engine}-final.png"
            page.screenshot(path=str(shot), full_page=False)
            result.raw["screenshot"] = str(shot)
            log(f"screenshot written to {shot}")
        except Exception as exc:  # noqa: BLE001
            log(f"screenshot failed: {exc}")

    if not location_ok:
        # The retailer never confirmed the requested delivery location, so whatever
        # the search returned is the catalogue for some *other* location. The rows are
        # real — real names, real prices — but they are not an answer to the question
        # that was asked, and there is no way for a consumer to tell that from the
        # data alone. Reporting this as success would be the silent substitution the
        # brief forbids, so it is a failure with the rows kept for inspection.
        # Engine A already treats this case the same way (LOCATION_NOT_SET); the two
        # engines agreeing matters more than either one being lenient.
        result.status = "FAILED"
        result.error_code = "LOCATION_NOT_SET"
        result.error_message = (
            f"{result.retailer} never confirmed a delivery location for pincode "
            f"{pincode}, so the {len(all_products)} product(s) below are for an unknown "
            "location. They are kept for inspection but are not a valid answer for "
            "this pincode."
        )
        for product in result.products:
            product.warnings.append(
                f"location unverified: delivery to {pincode} was never confirmed"
            )
    elif all_products:
        result.status = "COMPLETED"
    else:
        result.status = "FAILED"
        result.error_code = "NO_PRODUCTS_EXTRACTED"
        result.error_message = (
            "The browser ran but no products could be extracted. Check the run log: "
            "the location gate may not have been cleared, or the site's markup may "
            "not match the extraction heuristics. The captured JSON URLs in the log "
            "show what the page actually requested."
        )


def save_state(page: Any, context: Any, destination: Path, log: Callable[[str], None]) -> None:
    """Persist cookies and local storage so a later run can reuse the session."""
    try:
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        (context or page.context).storage_state(path=str(destination))
        log(f"saved session state to {destination}")
    except Exception as exc:  # noqa: BLE001
        log(f"could not save session state: {exc}")
