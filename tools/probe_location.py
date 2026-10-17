"""Discovery probe v3: set Blinkit's delivery location, then capture the product-search API.

What the earlier probes established, the hard way:

  * Blinkit AUTO-OPENS its location modal on landing when no location is set. There is
    nothing to click first — v2 failed because it tried to click "Select Location" while
    the modal's own overlay (`LocationDropDown__LocationOverlay`) was intercepting
    pointer events.
  * The modal's search box is the first `input[placeholder*=search]` on the page, which
    is why v1's "product search" was really hitting `/location/autoSuggest`.
  * Blinkit's location API is `/location/autoSuggest?query=&lat=&lng=&session_token=`,
    and the default lat/lng is Delhi NCR — the wrong geo for a Bengaluru pincode.

So the flow is: wait for the modal input, type the pincode, pick a suggestion, then the
catalogue loads and the real product search becomes available.

Read-only: sets a delivery location and runs a search. Never touches a cart.

    python tools/probe_location.py --pincode 560001 --query milk
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

RUNS = Path("runs/probe")
PRODUCT_HINTS = ("price", "mrp", "product_id", "inventory", "snippets", "selling_price")

# Stable styled-components prefixes. The trailing hash is generated, so match on the
# component name only.
MODAL_INPUT = "div[class*='LocationModalContainer'] input"
OVERLAY = "div[class*='LocationOverlay']"


def log(message: str) -> None:
    print(message, flush=True)


def shot(page, name: str) -> None:
    RUNS.mkdir(parents=True, exist_ok=True)
    try:
        page.screenshot(path=str(RUNS / f"{name}.png"))
        log(f"    screenshot -> {RUNS / f'{name}.png'}")
    except Exception as exc:  # noqa: BLE001
        log(f"    screenshot failed: {exc}")


def dump_inputs(page, label: str) -> list[dict]:
    inputs = page.eval_on_selector_all(
        "input",
        """els => els.map(e => {
            const r = e.getBoundingClientRect();
            const inModal = !!e.closest("div[class*='LocationModalContainer']");
            return {
                placeholder: e.placeholder || '',
                type: e.type,
                inLocationModal: inModal,
                visible: r.width > 0 && r.height > 0,
                cls: (e.className || '').toString().slice(0, 70)
            };
        })""",
    )
    log(f"    {label}: {json.dumps(inputs, indent=2)[:1800]}")
    return inputs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pincode", default="560001")
    parser.add_argument("--query", default="milk")
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

        # ---- 1. wait for the auto-opened location modal --------------------------
        log("\n[1] waiting for the location modal (it auto-opens)...")
        try:
            page.wait_for_selector(MODAL_INPUT, state="visible", timeout=40_000)
            log(f"    modal input found via {MODAL_INPUT!r}")
        except Exception as exc:  # noqa: BLE001
            log(f"    modal input not found: {str(exc)[:200]}")
            log("    falling back to any visible input")
            try:
                page.wait_for_selector("input", state="visible", timeout=15_000)
            except Exception:  # noqa: BLE001
                log("    no input at all; giving up")
                shot(page, "20-no-modal")
                return 1

        time.sleep(1.5)
        shot(page, "20-modal")
        dump_inputs(page, "inputs")

        # ---- 2. type the pincode ------------------------------------------------
        log(f"\n[2] typing {args.pincode} into the location modal...")
        box = page.locator(MODAL_INPUT).first
        if box.count() == 0:
            box = page.locator("input").first
        box.click()
        time.sleep(0.4)
        box.type(args.pincode, delay=150)
        log("    typed; waiting for suggestions...")
        time.sleep(5)
        shot(page, "21-suggestions")

        # ---- 3. pick the Bengaluru suggestion -----------------------------------
        log("\n[3] candidate suggestions:")
        suggestions = page.eval_on_selector_all(
            "div, li, button, span, p",
            """els => els
                .filter(e => e.children.length === 0 && (e.innerText || '').trim().length > 4)
                .map(e => (e.innerText || '').trim())
                .filter(t => /560001|bengaluru|bangalore/i.test(t))
                .filter((t, i, a) => a.indexOf(t) === i)
                .slice(0, 12)""",
        )
        for s in suggestions:
            log(f"    - {s!r}")

        picked = None
        # Prefer a suggestion that actually names the pincode.
        ordered = sorted(suggestions, key=lambda t: ("560001" not in t, len(t)))
        for text in ordered:
            try:
                page.get_by_text(text, exact=True).first.click(timeout=4000)
                picked = text
                log(f"    clicked {text!r}")
                break
            except Exception:  # noqa: BLE001
                continue

        if picked is None:
            log("    no suggestion clicked; pressing Enter")
            page.keyboard.press("Enter")

        log("    waiting for the catalogue to load...")
        time.sleep(10)
        shot(page, "22-after-location")

        # ---- 4. did the location actually take? ---------------------------------
        log("\n[4] checking whether the location was set...")
        try:
            page.wait_for_selector(OVERLAY, state="detached", timeout=20_000)
            log("    location modal is gone")
        except Exception:  # noqa: BLE001
            log("    location modal still present after 20s")

        body = page.inner_text("body")
        log(f"    url: {page.url}")
        log(f"    text: {body[:250].replace(chr(10), ' | ')}")
        location_set = "provide your delivery location" not in body.lower()
        log(f"    location appears set: {location_set}")

        # ---- 5. product search --------------------------------------------------
        log(f"\n[5] searching products for {args.query!r}...")
        before = len(responses)
        try:
            # The product search box is the one NOT inside the location modal.
            box = page.locator("input[placeholder*='search' i]").last
            box.click()
            time.sleep(0.4)
            box.type(args.query, delay=120)
            time.sleep(2)
            page.keyboard.press("Enter")
            log("    submitted")
        except Exception as exc:  # noqa: BLE001
            log(f"    search failed: {str(exc)[:160]}")

        time.sleep(12)
        shot(page, "23-search-results")
        log(f"    url: {page.url}")
        text = page.inner_text("body")
        log(f"    text: {text[:600].replace(chr(10), ' | ')}")

        # ---- 6. rank the captured JSON ------------------------------------------
        log("\n[6] JSON captured during the product search (ranked):")
        scored = []
        for entry in responses[before:]:
            blob = json.dumps(entry["body"], default=str).lower()
            score = sum(hint in blob for hint in PRODUCT_HINTS)
            if args.query.lower() in blob:
                score += 2
            scored.append((score, len(blob), entry["method"], entry["url"], entry))
        scored.sort(key=lambda row: row[0], reverse=True)

        for score, size, method, url, _ in scored[:12]:
            log(f"    score={score:<3} {size:>7}B  {method:<5} {url[:140]}")

        if scored and scored[0][0] > 0:
            best = scored[0][4]
            (RUNS / "search-json.json").write_text(
                json.dumps(best["body"], indent=2, default=str)[:400_000]
            )
            log(f"\n    best -> {RUNS / 'search-json.json'}")
            log(f"    url: {best['url']}")
            if isinstance(best["body"], dict):
                log(f"    top-level keys: {list(best['body'].keys())[:25]}")

        (RUNS / "endpoints-v3.json").write_text(
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
        log(f"    {len(responses)} endpoint(s) recorded")
        return 0
    finally:
        try:
            browser.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(main())
