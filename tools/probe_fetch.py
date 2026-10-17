"""Reconnaissance: can TinyFish Fetch reach each retailer's search page?

This is a *probe*, not an adapter. Its output is a reachability table, not product
data, and nothing it returns is ever fed to the optimizer.

Fetch is the cheapest TinyFish surface — 1,000 URLs/day free, no proxy or stealth
knob — so it is the right instrument for triage: it tells us in one call which
retailers are worth spending browser time on, and which are walled off before we
start. A ``bot_blocked`` error here is a *finding*, not a failure of the probe.

    python tools/probe_fetch.py
    python tools/probe_fetch.py --retailer blinkit --query milk

Free to run. Writes nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from basketpilot.config import load_settings  # noqa: E402

FETCH_URL = "https://api.fetch.tinyfish.ai"

# The search URL each retailer actually serves. These are the entry points a
# browser adapter would open, so probing them here is a fair preview of what a
# browser would face — with the caveat that Fetch has no stealth and no proxy,
# so a block here does not prove a browser would be blocked too.
RETAILERS: dict[str, str] = {
    "blinkit": "https://blinkit.com/s/?q={query}",
    "zepto": "https://www.zepto.com/search?query={query}",
    "jiomart": "https://www.jiomart.com/search/{query}",
    "instamart": "https://www.swiggy.com/instamart/search?query={query}",
    "bigbasket": "https://www.bigbasket.com/ps/?q={query}",
    "flipkart": "https://www.flipkart.com/search?q={query}&marketplace=GROCERY",
}

# What a bot wall looks like when it comes back as *content* rather than an error:
# Fetch succeeded, the page loaded, and the page is a challenge. Worth detecting
# separately from ``bot_blocked``, because the two have different fixes.
BLOCK_MARKERS = (
    "just a moment",
    "enable javascript and cookies",
    "checking your browser",
    "attention required",
    "access denied",
    "you have been blocked",
    "cf-challenge",
    "captcha",
    "are you a robot",
)


def classify(entry: dict) -> tuple[str, str]:
    """Return ``(verdict, detail)`` for one Fetch result or error entry."""
    if "error" in entry:
        return entry["error"], (entry.get("message") or "").strip()[:160]

    text = (entry.get("text") or "") if isinstance(entry.get("text"), str) else ""
    lowered = text.lower()
    for marker in BLOCK_MARKERS:
        if marker in lowered:
            return "blocked_page", f"page content looks like a challenge ({marker!r})"

    # A page with no text at all is not a win, even though Fetch returned 200.
    if len(text.strip()) < 200:
        return "empty_content", f"only {len(text.strip())} chars of text"

    title = (entry.get("title") or "").strip()
    return "reached", f"{len(text)} chars · {title[:80]!r}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", default="milk", help="Search term to probe with.")
    parser.add_argument(
        "--retailer",
        action="append",
        choices=sorted(RETAILERS),
        help="Probe only this retailer (repeatable). Default: all.",
    )
    args = parser.parse_args()

    settings = load_settings()
    if not settings.has_tinyfish_key:
        print("error: TINYFISH_API_KEY is not set in .env", file=sys.stderr)
        return 2

    keys = args.retailer or sorted(RETAILERS)
    urls = [RETAILERS[key].format(query=args.query) for key in keys]

    print(f"[fetch] probing {len(urls)} retailer search page(s) for {args.query!r}", file=sys.stderr)

    body = {
        "urls": urls,
        # Live, not cached: a cached challenge page would misreport reachability.
        "ttl": 0,
        "format": "markdown",
        "purpose": "Check whether a grocery retailer search page is reachable or bot-walled.",
        "per_url_timeout_ms": 60000,
    }

    try:
        with httpx.Client(timeout=httpx.Timeout(150.0, connect=15.0)) as client:
            response = client.post(
                FETCH_URL,
                headers={"X-API-Key": settings.tinyfish_api_key or ""},
                json=body,
            )
    except httpx.HTTPError as exc:
        print(f"error: fetch request failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if response.status_code != 200:
        print(
            f"error: HTTP {response.status_code} from Fetch API\n{response.text[:600]}",
            file=sys.stderr,
        )
        return 1

    payload = response.json()
    by_url = {entry["url"]: entry for entry in payload.get("results", [])}
    for entry in payload.get("errors", []):
        by_url[entry["url"]] = entry

    rows: list[dict] = []
    print("", file=sys.stderr)
    for key, url in zip(keys, urls):
        entry = by_url.get(url, {"error": "missing_from_response", "url": url})
        verdict, detail = classify(entry)
        rows.append({"retailer": key, "url": url, "verdict": verdict, "detail": detail})
        mark = "OK  " if verdict == "reached" else "BLOCK"
        print(f"  {mark} {key:<10} {verdict:<14} {detail}", file=sys.stderr)

    reached = [row["retailer"] for row in rows if row["verdict"] == "reached"]
    print(
        f"\n[fetch] reached {len(reached)}/{len(rows)}: {', '.join(reached) or 'none'}",
        file=sys.stderr,
    )

    # The machine-readable table, on stdout, so it can be piped.
    print(json.dumps({"query": args.query, "results": rows}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
