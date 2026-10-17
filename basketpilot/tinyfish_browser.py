"""Engine: TinyFish Browser API — a real browser, reached over CDP.

This is the engine Phase 2 reconnaissance proved out. Phase 1 concluded that
TinyFish could not reach Blinkit, but it had only tried the *Agent* API, which
drives a browser on TinyFish's side and reports back in prose. The Browser API is a
different product: it returns a ``cdp_url`` and hands the browser to us as plain
Playwright. That difference is the whole story, because it means the extraction
ladder proven in Phase 1 runs over it unchanged.

What the probes established (see ``tools/probe_browser.py`` and the README):

* The Browser API is enabled on this account (session created, ``201``).
* ``proxy_config.country_code: "IN"`` is accepted here, though the Agent API
  rejected the same value with HTTP 400. The two surfaces genuinely disagree; the
  docs' "any ISO 3166-1 alpha-2" claim holds for this one.
* The proxy really is an Indian residential IP — egress reported Mumbai,
  Maharashtra, AS55836 Reliance Jio Infocomm.
* Behind it, Blinkit's Cloudflare wall *passes*: the page returns its real title
  rather than an interstitial, and the search API answers ``200``.

So this module is thin on purpose. It creates a session, connects, and hands the
page to :func:`basketpilot.retailer_runner.drive_page` — the same code path the
local engine uses. Two engines, one set of extraction rules.

Costs money, so sessions are terminated on every exit path.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import httpx

from .cloak_client import Blocked as BlockedError
from .config import Settings
from .models import RetailerResult

BROWSER_URL = "https://api.browser.tinyfish.ai"

# Session creation takes 10-30s on TinyFish's side, so the timeout has to clear that
# comfortably or a working request looks like a hang.
CREATE_TIMEOUT = httpx.Timeout(90.0, connect=20.0)
DELETE_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# Default proxy exit country. India, because every target retailer is an Indian
# quick-commerce site whose catalogue, pricing and availability are all
# location-specific — a US exit would return US-shaped pages or nothing at all.
DEFAULT_COUNTRY = "IN"

# Inactivity timeout for the session. Generous: a full multi-query run with two
# attempts per query can spend several minutes on the location gate alone, and a
# session that dies mid-run would waste the whole run.
DEFAULT_SESSION_SECONDS = 900


class BrowserAPIUnavailable(RuntimeError):
    """The Browser API refused to create a session.

    Raised separately from a generic HTTP error because the two most likely causes
    need different responses from a human: ``404`` means the API is not enabled for
    the account (contact TinyFish support), while ``402`` means it is enabled but
    unfunded. Collapsing them into "HTTP error" would hide that distinction.
    """


class TinyFishBrowser:
    """A TinyFish browser session, usable as a context manager.

    ``with TinyFishBrowser(settings, country="IN") as session:`` creates the session
    on entry and terminates it on exit, including on an exception. The terminate
    call is best-effort and never masks the original error.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        country: str | None = DEFAULT_COUNTRY,
        session_seconds: int = DEFAULT_SESSION_SECONDS,
        verbose: bool = True,
        profile_id: str | None = None,
    ) -> None:
        self.settings = settings
        self.country = country
        self.session_seconds = session_seconds
        self.verbose = verbose
        # A warmed Browser Context Profile: cookies, local storage and the delivery
        # location, saved from a session a human logged into. Optional, because the
        # pipeline must still run cold — but when it is set and ignored, a run would
        # look identical to one that used it, which is the silent substitution this
        # project refuses to make. So the id is echoed in the session log.
        self.profile_id = profile_id
        self.session_id: str | None = None
        self.cdp_url: str | None = None
        self._client: httpx.Client | None = None

    def log(self, message: str) -> None:
        if self.verbose:
            print(f"  [tinyfish-browser] {message}", file=sys.stderr, flush=True)

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self.settings.tinyfish_api_key or ""}

    def __enter__(self) -> TinyFishBrowser:
        self._client = httpx.Client(timeout=CREATE_TIMEOUT)
        body: dict[str, Any] = {"timeout_seconds": self.session_seconds}
        if self.country:
            body["proxy_config"] = {
                "enabled": True,
                "type": "tinyfish",
                "country_code": self.country,
            }
        else:
            # Explicit rather than omitted: omitting the field would silently get the
            # default US residential proxy, which is not what "no proxy" means.
            body["proxy_config"] = {"enabled": False}

        if self.profile_id:
            body["use_profile"] = True
            body["profile_id"] = self.profile_id

        self.log(
            f"creating browser session (proxy country={self.country or 'direct'}"
            + (f", profile {self.profile_id}" if self.profile_id else ", cold profile")
            + ")…"
        )
        try:
            response = self._client.post(BROWSER_URL, headers=self._headers, json=body)
        except httpx.HTTPError as exc:
            self._close_client()
            raise BrowserAPIUnavailable(f"could not reach the Browser API: {exc}") from exc

        if response.status_code != 201:
            self._close_client()
            raise BrowserAPIUnavailable(self._explain(response))

        payload = response.json()
        self.session_id = payload.get("session_id")
        self.cdp_url = payload.get("cdp_url")
        if not self.session_id or not self.cdp_url:
            self._close_client()
            raise BrowserAPIUnavailable(
                f"session created but the response carried no cdp_url: {payload!r}"
            )
        self.log(f"session {self.session_id} ready")
        return self

    def _explain(self, response: httpx.Response) -> str:
        """Turn a Browser API error into something a human can act on."""
        code = response.status_code
        detail = response.text[:400]
        if code == 404:
            return (
                "the Browser API returned 404 NOT_FOUND, which per TinyFish's docs means "
                "the API is not enabled for this account. This is not a key problem — the "
                "same key works for other TinyFish surfaces. Contact TinyFish support to "
                f"have it enabled. Raw response: {detail}"
            )
        if code == 402:
            return (
                "the Browser API returned 402 INSUFFICIENT_CREDITS. The API is enabled but "
                f"the account balance is exhausted (minimum top-up is $10). {detail}"
            )
        if code == 401:
            return f"the Browser API rejected the key (401). Check TINYFISH_API_KEY. {detail}"
        return f"browser session creation failed: HTTP {code}. {detail}"

    def __exit__(self, *exc_info: object) -> None:
        self.terminate()

    def terminate(self) -> None:
        """End the session. Idempotent, and never raises."""
        if self._client is not None and self.session_id:
            try:
                response = self._client.delete(
                    f"{BROWSER_URL}/{self.session_id}", headers=self._headers
                )
                self.log(f"terminated session {self.session_id} (HTTP {response.status_code})")
            except httpx.HTTPError as exc:
                # A session left open costs money until it times out, so this is worth
                # saying out loud rather than swallowing.
                self.log(
                    f"could not terminate session {self.session_id} ({exc}); it will end "
                    "on its inactivity timeout instead"
                )
        self._close_client()

    def _close_client(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None


def search(
    *,
    settings: Settings,
    retailer: str,
    retailer_url: str,
    pincode: str,
    queries: list[str],
    country: str | None = DEFAULT_COUNTRY,
    screenshot_dir: Path | None = None,
    verbose: bool = True,
    profile_id: str | None = None,
) -> RetailerResult:
    """Search a retailer through a TinyFish-hosted browser.

    Returns a :class:`RetailerResult` in every case. A failure to obtain a browser
    is reported as ``ENGINE_UNAVAILABLE`` rather than raised, so a caller running
    several retailers can carry on with the others and report honestly about this
    one.
    """
    def log(message: str) -> None:
        if verbose:
            print(f"  [tinyfish-browser] {message}", file=sys.stderr, flush=True)

    result = RetailerResult(
        retailer=retailer,
        engine="tinyfish_browser",
        queries=queries,
        status="PENDING",
        delivery_pincode=pincode,
    )

    try:
        with TinyFishBrowser(
            settings, country=country, verbose=verbose, profile_id=profile_id
        ) as session:
            result.raw["session_id"] = session.session_id
            result.raw["proxy_country"] = country
            _drive(session.cdp_url or "", result, retailer_url, pincode, queries, screenshot_dir, log)
    except BrowserAPIUnavailable as exc:
        result.status = "FAILED"
        result.error_code = "ENGINE_UNAVAILABLE"
        result.error_message = str(exc)
    except BlockedError as exc:
        result.status = "FAILED"
        result.error_code = "SITE_BLOCKED"
        result.error_message = (
            f"The site served a bot challenge that the TinyFish browser did not clear: "
            f"{exc}. The proxy country was {country!r}; a different exit country or a "
            "warmed browser profile may behave differently."
        )
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        result.status = "FAILED"
        result.error_code = "BROWSER_ERROR"
        result.error_message = f"{type(exc).__name__}: {exc}"

    return result


def _drive(
    cdp_url: str,
    result: RetailerResult,
    retailer_url: str,
    pincode: str,
    queries: list[str],
    screenshot_dir: Path | None,
    log: Any,
) -> None:
    """Connect over CDP and run the shared extraction ladder against the page."""
    from playwright.sync_api import sync_playwright

    from .retailer_runner import drive_page

    with sync_playwright() as playwright:
        # connect_over_cdp attaches to a browser TinyFish owns. Closing it here would
        # close *their* browser, so teardown is left to session termination — the
        # `with TinyFishBrowser(...)` block above. That is also why the connection is
        # not wrapped in a try/finally that calls browser.close().
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()
        log("connected over CDP")

        drive_page(
            page,
            result=result,
            retailer_url=retailer_url,
            pincode=pincode,
            queries=queries,
            engine="tinyfish_browser",
            screenshot_dir=screenshot_dir,
            context=context,
            log=log,
        )
