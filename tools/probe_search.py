"""Discovery probe v4: find Blinkit's product-search API and the product card shape.

Probe v3 solved the location gate (auto-open modal, input placeholder "search delivery
location", pick the `560001` suggestion). It then failed on the product search because
the home page has NO `input[placeholder*=search]` — only the location modal has an
input. The home search bar is a div that opens an overlay.

Rather than fight that overlay, this probe navigates straight to Blinkit's search
route, `https://blinkit.com/s/?q=<query>`, which is a normal page load. That is both
simpler and closer to what the extraction code will do.

It records every JSON response, then dumps the DOM shape of one product card so the
extraction code can be written against the real structure.

Read-only: sets a delivery location and opens a search page. Never adds to a cart.

    python tools/probe_search.py --pincode 560001 --query milk
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

RUNS = Path("runs/probe")
PRODUCT_HINTS = ("price", "mrp", "product_id", "inventory", "snippets", "selling_price", "brand")

LOCATION_INPUT = "input[placeholder='search delivery location']"
OVERLAY = "div[class*='LocationOverlay']"


def log(message: str) -> None:
    print(message, flush=True)


def shot(page, name: str) -> None:
    RUNS.mkdir(parents=True, exist_ok=True)
    try:
        page.screenshot(path=str(RUNS / f"{name}.png"), full_page=False)
        log(f"    screenshot -> {RUNS / f'{name}.png'}")
    except Exception as exc:  # noqa: BLE001
        log(f"    screenshot failed: {exc}")


def set_location(page, pincode: str) -> bool:
    """The flow probe v3 proved works. Returns True if the location took."""
    log(f"[1] setting delivery location to {pincode}...")
    page.wait_for_selector(LOCATION_INPUT, state="visible", timeout=40_000)
    box = page.locator(LOCATION_INPUT).first
    box.click()
    time.sleep(0.4)
    box.type(pincode, delay=150)
    time.sleep(5)

    # Prefer the suggestion that is exactly the pincode — v3 showed that clicking the
    # bare '560001' row is what actually commits the location.
    clicked = False
    for text in (pincode,):
        try:
            page.get_by_text(text, exact=True).first.click(timeout=5000)
            log(f"    clicked suggestion {text!r}")
            clicked = True
            break
        except Exception:  # noqa: BLE001
            continue
    if not clicked:
        log("    exact pincode row not found; pressing Enter")
        page.keyboard.press("Enter")

    try:
        page.wait_for_selector(OVERLAY, state="detached", timeout=25_000)
        log("    location modal dismissed")
    except Exception:  # noqa: BLE001
        log("    location modal still present")

    body = page.inner_text("body")
    ok = "provide your delivery location" not in body.lower()
    log(f"    location set: {ok}")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pincode", default="560001")
    parser.add_argument("--queries", default="milk,bread,eggs")
    args = parser.parse_args()

    from cloakbrowser import launch

    log("launching stealth Chromium...")
    browser = launch(headless=False, humanize=True, timezone="Asia/Kolkata", locale="en-IN")

    responses: list[dict] = []

    def on_response(response):
        try:
            request = response.request
            if request.resource_type not in ("xhr", "fetch"):
                return
            content_type = (response.headers or {}).get("content-type", "")
            if "json" not in content_type.lower() or response.status >= 400:
                return
            responses.append(
                {
                    "url": response.url,
                    "method": request.method,
                    "status": response.status,
                    "headers": dict(request.headers),
                    "post_data": request.post_data,
                    "body": response.json(),
                }
            )
        except Exception:  # noqa: BLE001
            return

    try:
        page = browser.new_page()
        page.on("response", on_response)

        log("navigating to blinkit.com ...")
        page.goto("https://blinkit.com/", wait_until="domcontentloaded", timeout=60_000)

        if not set_location(page, args.pincode):
            shot(page, "30-location-failed")
            return 1

        time.sleep(3)

        # ---- 2. search each query in the same session --------------------------
        queries = [q.strip() for q in args.queries.split(",") if q.strip()]
        for query in queries:
            search_url = f"https://blinkit.com/s/?q={query}"
            log(f"\n[2] navigating to {search_url}")
            before = len(responses)
            page.goto(search_url, wait_until="domcontentloaded", timeout=60_000)
            time.sleep(9)
            shot(page, f"31-search-{query}")
            log(f"    url: {page.url}")

            text = page.inner_text("body")
            log(f"    text: {text[:700].replace(chr(10), ' | ')}")

            # ---- 3. rank the JSON captured on the search page ------------------
            log(f"\n[3] JSON captured for {query!r} (ranked):")
            scored = []
            for entry in responses[before:]:
                blob = json.dumps(entry["body"], default=str).lower()
                score = sum(hint in blob for hint in PRODUCT_HINTS)
                if query.lower() in blob:
                    score += 2
                scored.append((score, len(blob), entry["method"], entry["url"], entry))
            scored.sort(key=lambda row: row[0], reverse=True)

            for score, size, method, url, _ in scored[:15]:
                log(f"    score={score:<3} {size:>8}B  {method:<5} {url[:130]}")

            if scored and scored[0][0] > 0:
                best = scored[0][4]
                # Full body, no truncation — a sliced file is not valid JSON.
                out = RUNS / f"search-{query}.json"
                out.write_text(json.dumps(best["body"], indent=2, default=str))
                log(f"\n    best -> {out}  ({len(json.dumps(best['body']))} bytes)")
                log(f"    url: {best['url']}")
                log(f"    method: {best['method']}")
                log(f"    post_data: {str(best['post_data'])[:400]}")
                if isinstance(best["body"], dict):
                    log(f"    top-level keys: {list(best['body'].keys())[:25]}")

        # ---- 4. product card text, for cross-checking the JSON ---------------
        log("\n[4] product card text via DOM (cross-check):")
        cards = page.eval_on_selector_all(
            "a[href*='prid']",
            "els => els.slice(0, 4).map(e => (e.innerText || '').replace(/\\n/g, ' | '))",
        )
        for card in cards:
            log(f"    {card[:220]}")

        (RUNS / "endpoints-search.json").write_text(
            json.dumps(
                [
                    {"url": e["url"], "method": e["method"], "status": e["status"],
                     "headers": e["headers"], "post_data": e["post_data"]}
                    for e in responses
                ],
                indent=2,
                default=str,
            )
        )
        log(f"\n    {len(responses)} endpoint(s) recorded total")
        return 0
    finally:
        try:
            browser.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(main())
