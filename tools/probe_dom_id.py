"""Probe: does a Blinkit search card expose its product id in the DOM?

`_extract_from_dom()` is the fallback rung, used when the search JSON is unavailable.
It reads card text, which yields name/size/price but no product URL — the search page
has no `<a href>` product anchors, so there is nothing to read a URL from.

That leaves half the products in a typical run without a URL. Before accepting that as
a permanent limitation, check whether the id is reachable another way: a `data-*`
attribute, an `id`, or an `onclick`/react prop on the card. If it is, the DOM rung can
reconstruct the same `/prn/<slug>/prid/<id>` URL the JSON rung produces.

    python tools/probe_dom_id.py
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from basketpilot.cloak_client import SEARCH_URL_TEMPLATE, _launch  # noqa: E402

QUERY = "milk"

# Look for a numeric product id anywhere a card might carry one.
ID_PATTERNS = [
    re.compile(r'data-product[_-]?id="(\d+)"', re.I),
    re.compile(r'data-prid="(\d+)"', re.I),
    re.compile(r'product_id["\']?\s*[:=]\s*["\']?(\d+)', re.I),
    re.compile(r'/prid/(\d+)', re.I),
    re.compile(r'"id"\s*:\s*"?(\d{2,7})"?'),
]


def main() -> int:
    out_path = Path("runs") / "probe-dom-id.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    browser = _launch(
        headless=False, humanize=True, timezone="Asia/Kolkata", locale="en-IN"
    )
    report: dict[str, object] = {"query": QUERY}
    try:
        page = browser.new_page()
        page.set_default_timeout(30_000)
        page.goto(SEARCH_URL_TEMPLATE.format(query=QUERY), wait_until="domcontentloaded")
        time.sleep(7.0)
        print(f"  landed on {page.url!r} title={page.title()!r}", flush=True)

        # Grab the outerHTML of the first few rendered cards. A card is identified the
        # same way _extract_from_dom identifies one: it contains an ADD button and a ₹.
        cards = page.evaluate(
            """() => {
                 const out = [];
                 const divs = Array.from(document.querySelectorAll('div'));
                 for (const d of divs) {
                   const t = d.innerText || '';
                   if (t.includes('ADD') && t.includes('\\u20b9') && t.length < 400) {
                     out.push(d.outerHTML.slice(0, 4000));
                     if (out.length >= 5) break;
                   }
                 }
                 return out;
               }"""
        )
        print(f"  captured {len(cards)} card HTML sample(s)", flush=True)

        findings = []
        for index, html in enumerate(cards):
            hits: dict[str, list[str]] = {}
            for pattern in ID_PATTERNS:
                found = pattern.findall(html)
                if found:
                    hits[pattern.pattern] = sorted(set(found))[:5]
            findings.append({"card_index": index, "id_hits": hits})
            print(f"  card {index}: {hits or 'no id found'}", flush=True)

        # Also check whether any attribute anywhere in the document looks like a prid.
        attr_scan = page.evaluate(
            """() => {
                 const found = {};
                 for (const el of document.querySelectorAll('*')) {
                   for (const a of el.attributes || []) {
                     if (/prid|product.?id/i.test(a.name)) {
                       found[a.name] = (found[a.name] || 0) + 1;
                     }
                   }
                 }
                 return found;
               }"""
        )
        print(f"  document attribute scan: {attr_scan or 'no product-id attributes'}", flush=True)

        report["card_findings"] = findings
        report["attribute_scan"] = attr_scan
        report["card_html_samples"] = cards

        page.screenshot(path=str(Path("runs") / "probe-dom-id.png"))
    finally:
        try:
            browser.close()
        except Exception:  # noqa: BLE001
            pass

    out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten -> {out_path}")

    any_hit = any(f["id_hits"] for f in findings) or bool(attr_scan)
    print(
        "\nRESULT: "
        + (
            "a product id IS reachable from the DOM — the DOM rung can build URLs."
            if any_hit
            else "no product id is reachable from the DOM. A DOM-extracted row cannot "
            "carry a product URL; that limitation is real, not a bug."
        )
    )
    return 0 if any_hit else 1


if __name__ == "__main__":
    raise SystemExit(main())
