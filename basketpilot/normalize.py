"""Retailer registry, engine orchestration, and output normalisation.

Both engines produce the same :class:`~basketpilot.models.RetailerResult`, so
everything downstream is engine-agnostic. This module owns the fallback decision:
when Engine A fails in a way that another engine might survive, Engine B is tried
and the final result records which engine actually produced the data.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from .config import Settings
from .models import Product, RetailerResult

# Phase 1 targets one retailer. The others are listed so the fallback order is
# explicit and auditable rather than hidden in a comment.
RETAILERS: dict[str, dict[str, Any]] = {
    "blinkit": {
        "name": "Blinkit",
        "url": "https://blinkit.com/",
        "phase1_primary": True,
        "note": "Root URL returns HTTP 403 with cf-mitigated: challenge (Cloudflare).",
    },
    "zepto": {
        "name": "Zepto",
        "url": "https://www.zepto.com/",
        "phase1_primary": False,
        "note": "Documented fallback if Blinkit blocks. Root returned HTTP 202 via CloudFront.",
    },
}


class UnknownRetailer(ValueError):
    """Raised when a retailer has no configuration."""


def get_retailer(key: str) -> dict[str, Any]:
    try:
        return RETAILERS[key.lower()]
    except KeyError:
        known = ", ".join(sorted(RETAILERS))
        raise UnknownRetailer(f"Unknown retailer {key!r}. Known retailers: {known}") from None


def dedupe(products: list[Product]) -> list[Product]:
    """Drop exact duplicates by (query, name, package size), preserving order."""
    seen: set[tuple[str, str, str]] = set()
    unique: list[Product] = []
    for product in products:
        fingerprint = (
            product.query.lower().strip(),
            product.name.lower().strip(),
            (product.package_size or "").lower().strip(),
        )
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        unique.append(product)
    return unique


def merge(primary: RetailerResult, rescue: RetailerResult) -> RetailerResult:
    """Combine a failed primary run with a successful rescue run.

    The result keeps the rescue's data but records the primary failure, so the log
    shows both what went wrong and where the data actually came from.

    The primary's *own* diagnostics are carried across too, not just its error code.
    When Engine A reports a goal-level failure such as `LOCATION_NOT_SET`, the useful
    detail is in what the agent said it saw and did — its `notes`, its `location_set`
    flag and any warnings it raised. Keeping only the error code would leave the next
    person to read the log unable to tell "the site blocked it" from "the agent could
    not find the location box", which are different problems with different fixes.
    """
    failure: dict[str, Any] = {
        "engine": primary.engine,
        "status": primary.status,
        "error_code": primary.error_code,
        "error_message": primary.error_message,
        "run_id": primary.run_id,
        "num_of_steps": primary.num_of_steps,
        "warnings": list(primary.warnings),
    }
    # The agent's structured output, if it produced one. This is the primary engine's
    # view of the page and is the most direct evidence available about why it failed.
    final = primary.raw.get("final_response")
    if isinstance(final, dict) and final.get("result") is not None:
        failure["agent_result"] = final["result"]
    if primary.raw.get("pincode_observed"):
        failure["pincode_observed"] = primary.raw["pincode_observed"]

    rescue.raw["primary_engine_failure"] = failure
    rescue.warnings.insert(
        0,
        f"{primary.engine} failed ({primary.error_code or primary.status}); "
        f"data below came from {rescue.engine}",
    )
    return rescue


def search(
    *,
    retailer_key: str,
    queries: list[str],
    pincode: str,
    engine: str,
    settings: Settings,
    verbose: bool = True,
    browser_profile: str = "stealth",
    proxy_country: str | None = "IN",
    max_steps: int = 250,
    max_duration_seconds: int = 480,
    screenshot_dir: Path | None = None,
    storage_state: Path | None = None,
    save_storage_state: Path | None = None,
    headed: bool = False,
) -> RetailerResult:
    """Search a retailer, choosing engines according to ``engine``.

    ``engine`` is one of ``auto``, ``tinyfish`` or ``cloak``. ``auto`` runs TinyFish
    first and falls back to the local stealth browser only when the failure is one
    another engine could plausibly survive — never on a billing or auth error, which
    would otherwise be hidden behind a rescue that appears to work.
    """
    retailer = get_retailer(retailer_key)

    if engine == "cloak":
        return _run_cloak(
            retailer, queries, pincode, verbose, screenshot_dir,
            storage_state, save_storage_state, headed,
        )

    # Imported lazily so `--engine cloak` works without TinyFish configured.
    from .tinyfish_client import TinyFishClient

    if verbose:
        print(f"[engine] tinyfish → {retailer['name']}", file=sys.stderr, flush=True)

    with TinyFishClient(settings, verbose=verbose) as client:
        primary = client.search(
            retailer=retailer["name"],
            retailer_url=retailer["url"],
            pincode=pincode,
            queries=queries,
            browser_profile=browser_profile,
            proxy_country=proxy_country,
            max_steps=max_steps,
            max_duration_seconds=max_duration_seconds,
        )

        if engine == "tinyfish":
            return primary

        if primary.succeeded:
            return primary

        if not client.should_fall_back(primary):
            if verbose:
                print(
                    f"[engine] not falling back: {primary.error_code} is a configuration "
                    "or billing problem, not a block.",
                    file=sys.stderr,
                    flush=True,
                )
            return primary

        if verbose:
            print(
                f"[engine] tinyfish failed ({primary.error_code or primary.status}) — "
                "escalating to local stealth browser",
                file=sys.stderr,
                flush=True,
            )

        rescue = _run_cloak(
            retailer, queries, pincode, verbose, screenshot_dir,
            storage_state, save_storage_state, headed,
        )
        return merge(primary, rescue) if rescue.succeeded else _both_failed(primary, rescue)


def _run_cloak(
    retailer: dict[str, Any],
    queries: list[str],
    pincode: str,
    verbose: bool,
    screenshot_dir: Path | None = None,
    storage_state: Path | None = None,
    save_storage_state: Path | None = None,
    headed: bool = False,
) -> RetailerResult:
    from .cloak_client import search as cloak_search

    if verbose:
        print(f"[engine] cloak → {retailer['name']}", file=sys.stderr, flush=True)

    return cloak_search(
        retailer=retailer["name"],
        retailer_url=retailer["url"],
        pincode=pincode,
        queries=queries,
        headless=not headed,
        screenshot_dir=screenshot_dir,
        storage_state=storage_state,
        save_storage_state=save_storage_state,
        verbose=verbose,
    )


def _both_failed(primary: RetailerResult, rescue: RetailerResult) -> RetailerResult:
    """Both engines failed — report the primary diagnosis, keeping the rescue detail."""
    rescue.raw["primary_engine_failure"] = {
        "engine": primary.engine,
        "status": primary.status,
        "error_code": primary.error_code,
        "error_message": primary.error_message,
    }
    rescue.warnings.insert(
        0,
        f"Both engines failed. {primary.engine}: {primary.error_code or primary.status}. "
        f"{rescue.engine}: {rescue.error_code or rescue.status}.",
    )
    return rescue
