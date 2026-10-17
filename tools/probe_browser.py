"""Reconnaissance: does the TinyFish Browser API reach Blinkit over plain CDP?

This is the decisive probe for Phase 2. Everything else in the plan branches on it.

It answers three questions the documentation could not settle:

1. Is the Browser API enabled on this account? (``404 NOT_FOUND`` if not.)
2. Does ``proxy_config.country_code: "IN"`` work here? The Browser API docs say
   "any ISO 3166-1 alpha-2 code", but the *Agent* API rejected ``IN`` with HTTP
   400 in Phase 1. If the two surfaces disagree, that is worth knowing precisely.
3. Does a real Chromium behind an Indian residential IP get past Blinkit's
   Cloudflare wall — the wall that stopped the Agent API?

Unlike the Agent API, this is plain CDP, so the *proven* Phase 1 extraction
approach can run over it unchanged. That is why a yes here changes the
architecture: TinyFish becomes the execution layer rather than a blocked one.

    python tools/probe_browser.py                    # create, test, terminate
    python tools/probe_browser.py --country IN
    python tools/probe_browser.py --keep             # leave the session open

Costs money (a browser session). Terminates the session on every exit path.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from basketpilot.config import load_settings  # noqa: E402

BROWSER_URL = "https://api.browser.tinyfish.ai"

BLOCK_MARKERS = (
    "just a moment",
    "enable javascript and cookies",
    "checking your browser",
    "attention required",
    "access denied",
    "you have been blocked",
    "sorry, you have been blocked",
    "cf-challenge",
    "are you a robot",
)


def create_session(client: httpx.Client, api_key: str, country: str | None, url: str) -> dict:
    body: dict = {"url": url, "timeout_seconds": 300}
    if country:
        body["proxy_config"] = {"enabled": True, "type": "tinyfish", "country_code": country}
    else:
        body["proxy_config"] = {"enabled": False}

    # Session creation takes 10-30s, so the client timeout must clear that comfortably.
    response = client.post(BROWSER_URL, headers={"X-API-Key": api_key}, json=body)
    if response.status_code != 201:
        raise RuntimeError(
            f"session creation failed: HTTP {response.status_code}\n{response.text[:600]}"
        )
    return response.json()


def terminate(client: httpx.Client, api_key: str, session_id: str) -> str:
    try:
        response = client.delete(
            f"{BROWSER_URL}/{session_id}", headers={"X-API-Key": api_key}
        )
        return f"HTTP {response.status_code}"
    except httpx.HTTPError as exc:
        return f"failed: {type(exc).__name__}: {exc}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--country", default="IN", help="Proxy exit country (default IN).")
    parser.add_argument("--no-proxy", action="store_true", help="Connect directly, no proxy.")
    parser.add_argument("--url", default="https://blinkit.com/s/?q=milk")
    parser.add_argument("--query", default="milk", help="Query to watch for in the search API.")
    parser.add_argument("--keep", action="store_true", help="Do not terminate the session.")
    parser.add_argument("--settle", type=float, default=12.0, help="Seconds to let the page run.")
    args = parser.parse_args()

    settings = load_settings()
    if not settings.has_tinyfish_key:
        print("error: TINYFISH_API_KEY is not set in .env", file=sys.stderr)
        return 2

    country = None if args.no_proxy else args.country
    verdict: dict = {"country": country, "url": args.url}

    with httpx.Client(timeout=httpx.Timeout(90.0, connect=20.0)) as client:
        print(f"[browser] creating session (country={country or 'direct'})…", file=sys.stderr)
        try:
            session = create_session(client, settings.tinyfish_api_key or "", country, args.url)
        except (RuntimeError, httpx.HTTPError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            # A 404 here is the documented "Browser API not enabled" signal. Say so
            # plainly rather than leaving the reader to look up the code.
            if "404" in str(exc):
                print(
                    "      404 NOT_FOUND on session creation means the Browser API is not "
                    "enabled for this account (per the docs, contact support).",
                    file=sys.stderr,
                )
            return 1

        session_id = session.get("session_id")
        cdp_url = session.get("cdp_url")
        verdict["session_id"] = session_id
        print(f"[browser] session {session_id}", file=sys.stderr)
        print(f"[browser] cdp_url  {cdp_url}", file=sys.stderr)

        try:
            verdict.update(drive(cdp_url, args.query, args.settle))
        except Exception as exc:  # noqa: BLE001 - a probe reports, it does not crash
            verdict["error"] = f"{type(exc).__name__}: {exc}"
            print(f"error: driving the session failed: {verdict['error']}", file=sys.stderr)
        finally:
            if args.keep:
                print(f"[browser] left session {session_id} open (--keep)", file=sys.stderr)
            else:
                print(
                    f"[browser] terminated session: {terminate(client, settings.tinyfish_api_key or '', session_id)}",
                    file=sys.stderr,
                )

    print(json.dumps(verdict, indent=2, default=str))
    return 0 if verdict.get("verdict") == "reached" else 1


def drive(cdp_url: str, query: str, settle: float) -> dict:
    """Connect over CDP and report what Blinkit actually served."""
    from playwright.sync_api import sync_playwright

    out: dict = {}
    captured: list[dict] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()

        # Record every response the page makes. A JSON 200 from Blinkit's search
        # endpoint is the strongest possible evidence — it means the site served
        # real data to this browser, not a challenge page.
        def on_response(response) -> None:
            try:
                url = response.url
                if "/v1/layout/search" not in url and "/v1/layout/" not in url:
                    return
                captured.append({"url": url[:160], "status": response.status})
            except Exception:  # noqa: BLE001
                return

        context.on("response", on_response)

        try:
            page.goto("https://blinkit.com/", wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:  # noqa: BLE001
            out["goto_warning"] = f"{type(exc).__name__}: {exc}"

        # The page is given real time to run: Cloudflare's interstitial clears on its
        # own if it is going to, and the SPA needs a moment to boot.
        page.wait_for_timeout(int(settle * 1000))

        out["title"] = page.title()
        try:
            body_text = page.inner_text("body")
        except Exception:  # noqa: BLE001
            body_text = ""
        out["body_chars"] = len(body_text)

        lowered = body_text.lower()
        hit = next((m for m in BLOCK_MARKERS if m in lowered), None)
        out["block_marker"] = hit
        out["responses"] = captured
        out["search_api_ok"] = any(entry["status"] == 200 for entry in captured)

        if out["search_api_ok"]:
            out["verdict"] = "reached"
        elif hit:
            out["verdict"] = "blocked"
        else:
            out["verdict"] = "loaded_but_no_search_api"

        try:
            browser.close()
        except Exception:  # noqa: BLE001
            pass

    return out


if __name__ == "__main__":
    raise SystemExit(main())
