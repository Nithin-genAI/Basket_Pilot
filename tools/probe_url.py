"""Probe: does the constructed Blinkit product URL actually resolve?

`_product_url()` in `basketpilot/cloak_client.py` synthesises a product URL as

    https://blinkit.com/prn/<slug>/prid/<id>

from the product id and name found in the search JSON. The slug is cosmetic and the
id is the real key, but that is a *belief* about Blinkit's routing until it is
observed. This probe tests it against the live site, using real product ids captured
from a real search response, and reports what the page actually shows.

It also re-checks the claim that a search page has no product `<a href>` anchors
(which is why DOM extraction reads card text instead of links).

    python tools/probe_url.py

Writes runs/probe-url.log-style output plus runs/probe-url.json.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from basketpilot.cloak_client import (  # noqa: E402
    SEARCH_URL_TEMPLATE,
    _launch,
    _product_url,
)

# (product_id, name) pairs taken verbatim from a captured Blinkit search response,
# so the probe exercises exactly what the extractor would emit.
CASES = [
    ("477483", "Eggoz Nutrition White Protein Rich Eggs (30 pieces)"),
    ("498143", "Licious Farm Fresh Classic White Protein Rich Eggs"),
    ("205", "Bajaj S.K. White Protein Rich Eggs"),
]

SEARCH_QUERY = "eggs"


def _log(message: str) -> None:
    print(f"  {message}", flush=True)


def main() -> int:
    out_path = Path("runs") / "probe-url.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    report: dict[str, object] = {"cases": [], "anchor_check": None}

    browser = _launch(
        headless=False,
        humanize=True,
        timezone="Asia/Kolkata",
        locale="en-IN",
    )
    try:
        page = browser.new_page()
        page.set_default_timeout(30_000)

        _log(f"navigating to {SEARCH_URL_TEMPLATE.format(query=SEARCH_QUERY)}")
        page.goto(SEARCH_URL_TEMPLATE.format(query=SEARCH_QUERY), wait_until="domcontentloaded")
        time.sleep(6.0)
        _log(f"landed on {page.url!r} title={page.title()!r}")

        # Claim 2: a search results page carries no product <a href> anchors.
        anchors = page.evaluate(
            """() => Array.from(document.querySelectorAll('a[href]'))
                 .map(a => a.getAttribute('href'))
                 .filter(h => h && (h.includes('/prn/') || h.includes('/prid/')))"""
        )
        report["anchor_check"] = {
            "product_anchors_on_search_page": len(anchors),
            "sample": anchors[:5],
        }
        _log(f"product <a href> anchors on the search page: {len(anchors)}")

        # Claim 1: the synthesised product URL resolves and shows that product.
        for product_id, name in CASES:
            url = _product_url(product_id, name)
            case: dict[str, object] = {"product_id": product_id, "name": name, "url": url}
            _log(f"checking {url}")
            try:
                response = page.goto(url, wait_until="domcontentloaded")
                time.sleep(4.0)
                title = page.title()
                body = page.inner_text("body")[:4000]
                # The product name is what the page should show if routing worked.
                # Compare on a distinctive fragment rather than the whole string,
                # since the PDP reformats the title.
                fragment = name.split("(")[0].strip()
                case.update(
                    {
                        "http_status": response.status if response else None,
                        "final_url": page.url,
                        "title": title,
                        "shows_product_name": fragment.lower() in body.lower(),
                        "shows_add_button": "ADD" in body,
                        "looks_like_404": any(
                            marker in body.lower()
                            for marker in ("page not found", "404", "something went wrong")
                        ),
                    }
                )
                _log(
                    f"  http={case['http_status']} url={case['final_url']} "
                    f"name_shown={case['shows_product_name']} add={case['shows_add_button']} "
                    f"404ish={case['looks_like_404']}"
                )
            except Exception as exc:  # noqa: BLE001
                case["error"] = f"{type(exc).__name__}: {exc}"
                _log(f"  ERROR {case['error']}")
            report["cases"].append(case)

        page.screenshot(path=str(Path("runs") / "probe-url.png"))
        _log("screenshot -> runs/probe-url.png")
    finally:
        try:
            browser.close()
        except Exception:  # noqa: BLE001
            pass

    out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten -> {out_path}")

    resolved = [c for c in report["cases"] if c.get("shows_product_name")]  # type: ignore[union-attr]
    print(
        f"\nRESULT: {len(resolved)}/{len(CASES)} constructed URL(s) resolved to the "
        "product page."
    )
    return 0 if len(resolved) == len(CASES) else 1


if __name__ == "__main__":
    raise SystemExit(main())
