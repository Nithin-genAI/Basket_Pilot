"""Smoke test: does CloakBrowser launch, and can it clear Blinkit's Cloudflare challenge?

This is deliberately standalone and dependency-free so it can be run before the rest
of BasketPilot works. It only navigates and reports — it never submits a form, and it
never writes to the target site.

    python tools/smoke_cloak.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

TARGET = "https://blinkit.com/"


def main() -> int:
    print(f"python: {sys.version.split()[0]}")

    try:
        import cloakbrowser
        from cloakbrowser import launch
    except ImportError as exc:
        print(f"FAIL: cloakbrowser not importable: {exc}")
        return 2

    print(f"cloakbrowser: {getattr(cloakbrowser, '__version__', 'unknown')}")

    try:
        from cloakbrowser.config import CHROMIUM_VERSION, get_chromium_version

        print(f"free binary version: {CHROMIUM_VERSION} (platform: {get_chromium_version()})")
    except Exception as exc:  # noqa: BLE001
        print(f"(could not read binary version: {exc})")

    print("\nlaunching stealth Chromium (first run downloads ~200MB, be patient)...")
    started = time.monotonic()
    try:
        browser = launch(headless=False, humanize=True, timezone="Asia/Kolkata", locale="en-IN")
    except Exception as exc:  # noqa: BLE001
        print(f"\nFAIL: launch raised {type(exc).__name__}: {exc}")
        print("\nIf this is a license error, the keyless free binary should be used instead.")
        print("If it is a quarantine error on macOS:  xattr -cr ~/.cloakbrowser")
        return 3

    print(f"launched in {time.monotonic() - started:.1f}s")

    try:
        page = browser.new_page()
        print(f"\nnavigating to {TARGET} ...")
        response = page.goto(TARGET, wait_until="domcontentloaded", timeout=60_000)
        status = getattr(response, "status", None)
        headers = dict(getattr(response, "headers", None) or {})
        mitigated = headers.get("cf-mitigated")
        print(f"  HTTP {status}  cf-mitigated={mitigated!r}  server={headers.get('server')!r}")

        time.sleep(5)
        title = page.title()
        url = page.url
        html = page.content()

        print(f"  title: {title!r}")
        print(f"  url:   {url!r}")
        print(f"  html:  {len(html)} bytes")

        blocked = (
            str(mitigated).lower() == "challenge"
            or "__cf_chl_" in url
            or "challenge-platform" in html
            or "Just a moment" in title
        )

        shot = Path("runs/smoke-cloak.png")
        shot.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(shot))
        print(f"  screenshot: {shot}")

        # What does the page actually look like? Useful for planning selectors.
        body_text = page.inner_text("body")[:400].replace("\n", " | ")
        print(f"\n  visible text: {body_text}")

        if blocked:
            print("\nRESULT: BLOCKED — Cloudflare challenge was served and did not clear.")
            return 1

        print("\nRESULT: PASSED the challenge — real page content rendered.")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"\nFAIL: navigation raised {type(exc).__name__}: {exc}")
        return 4
    finally:
        try:
            browser.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(main())
