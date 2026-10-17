"""Discovery probe: learn Blinkit's real location flow, search flow and JSON endpoints.

This exists because guessing selectors and endpoint URLs is how these integrations fail.
It drives the site like a person would, screenshots each step, and records the JSON the
page actually fetches — so the extraction code can be written against observed reality.

Read-only: it navigates, sets a delivery pincode and runs a search. It never adds to a
cart and never places an order.

    python tools/probe_blinkit.py --pincode 560001 --query milk
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

RUNS = Path("runs/probe")
PRODUCT_HINTS = ("price", "mrp", "product_id", "inventory", "snippets", "brand", "unit")


def log(message: str) -> None:
    print(message, flush=True)


def shot(page, name: str) -> Path:
    RUNS.mkdir(parents=True, exist_ok=True)
    path = RUNS / f"{name}.png"
    try:
        page.screenshot(path=str(path))
        log(f"    screenshot -> {path}")
    except Exception as exc:  # noqa: BLE001
        log(f"    screenshot failed: {exc}")
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pincode", default="560001")
    parser.add_argument("--query", default="milk")
    parser.add_argument("--headed", action="store_true", default=True)
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
        time.sleep(5)
        shot(page, "01-landing")

        # ---- Step 1: open the location gate -------------------------------------
        log("\n[1] looking for the location control...")
        candidates = page.eval_on_selector_all(
            "*",
            """els => els
                .filter(e => e.children.length === 0
                          && /select location|delivery location|change location/i.test(e.innerText || ''))
                .map(e => ({
                    tag: e.tagName,
                    text: (e.innerText || '').trim().slice(0, 60),
                    cls: (e.className || '').toString().slice(0, 120),
                    parentCls: (e.parentElement?.className || '').toString().slice(0, 120)
                }))""",
        )
        log(f"    candidate elements: {json.dumps(candidates, indent=2)[:1200]}")

        clicked = False
        for label in ("Select Location", "Delivery in", "Select location"):
            try:
                locator = page.get_by_text(label, exact=False).first
                if locator.count() > 0:
                    locator.click(timeout=5000)
                    log(f"    clicked {label!r}")
                    clicked = True
                    break
            except Exception as exc:  # noqa: BLE001
                log(f"    click {label!r} failed: {str(exc)[:120]}")

        time.sleep(3)
        shot(page, "02-after-location-click")

        # ---- Step 2: what inputs are now on screen? ------------------------------
        log("\n[2] inputs now present:")
        inputs = page.eval_on_selector_all(
            "input",
            """els => els.map(e => ({
                type: e.type, name: e.name,
                placeholder: e.placeholder,
                cls: (e.className || '').toString().slice(0, 120)
            }))""",
        )
        log(f"    {json.dumps(inputs, indent=2)[:1500]}")

        # ---- Step 3: enter the pincode -------------------------------------------
        log(f"\n[3] entering pincode {args.pincode}...")
        typed = False
        for selector in (
            "input[placeholder*='pincode' i]",
            "input[placeholder*='search' i]",
            "input[type='text']",
            "input",
        ):
            try:
                locator = page.locator(selector).first
                if locator.count() > 0 and locator.is_visible():
                    locator.click()
                    time.sleep(0.5)
                    locator.type(args.pincode, delay=120)
                    log(f"    typed into {selector!r}")
                    typed = True
                    break
            except Exception as exc:  # noqa: BLE001
                log(f"    {selector!r} failed: {str(exc)[:100]}")

        if not typed:
            log("    could not find an input to type into")
        time.sleep(4)
        shot(page, "03-pincode-typed")

        # ---- Step 4: list the suggestions ---------------------------------------
        log("\n[4] suggestion rows visible after typing:")
        suggestions = page.eval_on_selector_all(
            "div, li, button, span",
            """els => els
                .filter(e => e.children.length === 0 && (e.innerText || '').trim().length > 3)
                .map(e => (e.innerText || '').trim())
                .filter(t => /560001|bengaluru|bangalore/i.test(t))
                .slice(0, 15)""",
        )
        log(f"    {json.dumps(suggestions, indent=2)[:1200]}")

        # ---- Step 5: pick the first matching suggestion -------------------------
        log("\n[5] selecting a suggestion...")
        picked = False
        for text in suggestions:
            try:
                locator = page.get_by_text(text, exact=False).first
                locator.click(timeout=4000)
                log(f"    clicked suggestion {text!r}")
                picked = True
                break
            except Exception:  # noqa: BLE001
                continue
        if not picked:
            log("    no suggestion clicked; pressing Enter instead")
            try:
                page.keyboard.press("Enter")
            except Exception:  # noqa: BLE001
                pass

        time.sleep(6)
        shot(page, "04-location-set")
        log(f"    url now: {page.url}")
        body = page.inner_text("body")[:300].replace("\n", " | ")
        log(f"    page text: {body}")

        # ---- Step 6: run the search ---------------------------------------------
        log(f"\n[6] searching for {args.query!r}...")
        before = len(responses)
        searched = False
        try:
            search = page.locator("input[placeholder*='search' i]").first
            search.click()
            time.sleep(0.5)
            search.type(args.query, delay=100)
            time.sleep(1.5)
            page.keyboard.press("Enter")
            searched = True
            log("    search submitted")
        except Exception as exc:  # noqa: BLE001
            log(f"    search box failed: {str(exc)[:150]}")

        time.sleep(8)
        shot(page, "05-search-results")
        log(f"    url now: {page.url}")

        # ---- Step 7: rank the JSON we captured ----------------------------------
        log("\n[7] JSON responses captured (ranked by product-likeness):")
        scored = []
        for entry in responses[before:] if searched else responses:
            blob = json.dumps(entry["body"], default=str).lower()
            score = sum(hint in blob for hint in PRODUCT_HINTS)
            if args.query.lower() in blob:
                score += 2
            scored.append((score, len(blob), entry["method"], entry["url"], entry))
        scored.sort(key=lambda row: row[0], reverse=True)

        for score, size, method, url, _ in scored[:12]:
            log(f"    score={score:<3} {size:>7}B  {method:<5} {url[:130]}")

        # Dump the best candidate so its real shape can be inspected.
        if scored and scored[0][0] > 0:
            best = scored[0][4]
            RUNS.mkdir(parents=True, exist_ok=True)
            out = RUNS / "best-json.json"
            out.write_text(json.dumps(best["body"], indent=2, default=str)[:400_000])
            log(f"\n    best candidate written to {out}")
            log(f"    url:  {best['url']}")
            log(f"    keys: {list(best['body'].keys())[:20] if isinstance(best['body'], dict) else type(best['body'])}")
            log(f"    request headers: {json.dumps(best['headers'], indent=2)[:600]}")

        # Record every captured endpoint for later replay.
        summary = [
            {"url": e["url"], "method": e["method"], "status": e["status"],
             "headers": e["headers"], "post_data": e["post_data"]}
            for e in responses
        ]
        (RUNS / "endpoints.json").write_text(json.dumps(summary, indent=2, default=str))
        log(f"    all {len(summary)} endpoint(s) recorded to {RUNS / 'endpoints.json'}")

        results_text = page.inner_text("body")[:600].replace("\n", " | ")
        log(f"\n[8] results page text: {results_text}")

        return 0
    finally:
        try:
            browser.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(main())
