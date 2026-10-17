"""BasketPilot command line interface.

Usage:
    python -m basketpilot "milk, bread, eggs" --retailer blinkit --pincode 560001

Exit codes:
    0  products were extracted and validated
    1  the run failed, or produced no products (a diagnosis is printed)
    2  configuration problem, e.g. a missing API key
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import ConfigError, Settings, load_settings, require_tinyfish_key
from .models import RetailerResult
from .normalize import UnknownRetailer, dedupe, get_retailer, search

EXIT_OK = 0
EXIT_RUN_FAILED = 1
EXIT_CONFIG_ERROR = 2


def parse_queries(raw: list[str]) -> list[str]:
    """Split comma-separated arguments into a clean query list."""
    queries: list[str] = []
    for chunk in raw:
        for part in chunk.split(","):
            item = part.strip()
            if item and item not in queries:
                queries.append(item)
    return queries


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="basketpilot",
        description=(
            "Search an Indian quick-commerce retailer for a shopping list and return "
            "validated structured product data."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            '  python -m basketpilot "milk, bread, eggs" --retailer blinkit --pincode 560001\n'
            '  python -m basketpilot "milk" --engine cloak      # local stealth browser, no API key\n'
            '  python -m basketpilot "milk" --engine tinyfish   # TinyFish only\n'
        ),
    )
    parser.add_argument(
        "queries",
        nargs="+",
        help="Items to search for, e.g. \"milk, bread, eggs\" or several separate arguments.",
    )
    parser.add_argument("--retailer", default="blinkit", help="Retailer key (default: blinkit).")
    parser.add_argument(
        "--pincode",
        default="560001",
        help="Delivery pincode; quick-commerce sites hide their catalogue without one.",
    )
    parser.add_argument(
        "--engine",
        choices=("auto", "tinyfish", "cloak"),
        default="auto",
        help="auto = TinyFish first, local stealth browser on block (default).",
    )
    parser.add_argument(
        "--browser-profile",
        choices=("lite", "stealth"),
        default="stealth",
        help="TinyFish browser profile (default: stealth, required for bot-protected sites).",
    )
    parser.add_argument(
        "--proxy-country",
        default="IN",
        help="TinyFish proxy country code. Pass 'none' to disable the proxy.",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=250,
        help="TinyFish max_steps (default 250; TinyFish's own default is 150, which is "
        "tight for three searches plus a location gate).",
    )
    parser.add_argument(
        "--max-duration",
        type=int,
        default=480,
        help="TinyFish max_duration_seconds (default 480). Also bounds our own polling, "
        "so a hung run cannot eat the whole session.",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run the local stealth browser with a visible window (useful for diagnosis).",
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=None,
        help="Load saved browser session state (cookies/localStorage) to skip the "
        "challenge and location gate. Pair with --save-state to warm one.",
    )
    parser.add_argument(
        "--save-state",
        type=Path,
        default=None,
        help="Write browser session state here after the run, so later runs can reuse "
        "it via --state.",
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress progress logging.")
    parser.add_argument(
        "--json-only",
        action="store_true",
        help="Print only the JSON result, with no surrounding narrative.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write the JSON result here instead of stdout.",
    )
    return parser


def write_run_log(
    result: RetailerResult,
    *,
    runs_dir: Path,
    payload: dict,
) -> Path:
    """Persist the full run record so any failure can be diagnosed after the fact."""
    runs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = runs_dir / f"{stamp}-{result.retailer.lower().replace(' ', '-')}-{result.engine}.json"
    path.write_text(
        json.dumps({"request": payload, "result": result.to_dict()}, indent=2, default=str),
        encoding="utf-8",
    )
    return path


def report(result: RetailerResult, *, log_path: Path, quiet: bool) -> None:
    """Print a human-readable summary to stderr, leaving stdout for JSON."""
    if quiet:
        return
    stream = sys.stderr
    print("", file=stream)
    print(f"── {result.retailer} via {result.engine} ──", file=stream)
    print(f"   status      : {result.status}", file=stream)
    print(f"   products    : {len(result.products)}", file=stream)
    if result.run_id:
        print(f"   run_id      : {result.run_id}", file=stream)
    if result.num_of_steps is not None:
        print(f"   steps       : {result.num_of_steps}", file=stream)
    if result.request_id:
        print(f"   request_id  : {result.request_id}", file=stream)
    print(f"   diagnosis   : {result.diagnosis}", file=stream)
    print(f"   run log     : {log_path}", file=stream)

    for warning in result.warnings:
        print(f"   ! {warning}", file=stream)

    if result.products:
        print("", file=stream)
        for product in result.products:
            price = f"₹{product.price_numeric:g}" if product.price_numeric is not None else "price n/a"
            size = f" · {product.package_size}" if product.package_size else ""
            # Blinkit's own product name usually already starts with the brand
            # ("Amul Taaza Toned Milk"), so prefixing it again would print it twice.
            brand = ""
            if product.brand and not product.name.lower().startswith(product.brand.lower()):
                brand = f"{product.brand} "
            print(f"   [{product.query}] {brand}{product.name}{size} — {price}", file=stream)
            for warning in product.warnings:
                print(f"       ! {warning}", file=stream)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    queries = parse_queries(args.queries)
    if not queries:
        print("error: no search terms given", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    verbose = not args.quiet

    try:
        settings: Settings = load_settings()
        retailer = get_retailer(args.retailer)
        # Fail fast on a missing key, before any browser is launched.
        if args.engine in ("auto", "tinyfish"):
            require_tinyfish_key(settings)
    except (ConfigError, UnknownRetailer) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    proxy_country = None if args.proxy_country.lower() in ("none", "", "off") else args.proxy_country

    if verbose:
        # stderr throughout: stdout carries the JSON payload and nothing else, so that
        # `--json-only` can be piped straight into a parser. The engines' progress logs
        # follow the same rule.
        print(
            f"[basketpilot] {retailer['name']} · queries={queries} · pincode={args.pincode} "
            f"· engine={args.engine}",
            file=sys.stderr,
            flush=True,
        )
        if retailer.get("note"):
            print(f"[basketpilot] note: {retailer['note']}", file=sys.stderr, flush=True)

    try:
        result = search(
            retailer_key=args.retailer,
            queries=queries,
            pincode=args.pincode,
            engine=args.engine,
            settings=settings,
            verbose=verbose,
            browser_profile=args.browser_profile,
            proxy_country=proxy_country,
            max_steps=args.max_steps,
            max_duration_seconds=args.max_duration,
            screenshot_dir=settings.runs_dir,
            storage_state=args.state,
            save_storage_state=args.save_state,
            headed=args.headed,
        )
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    result.products = dedupe(result.products)

    payload = {
        "retailer": result.retailer,
        "engine": result.engine,
        "delivery_pincode": result.delivery_pincode,
        "queries": queries,
        "engine_request": {
            "browser_profile": args.browser_profile,
            "proxy_country": proxy_country,
            "max_steps": args.max_steps,
            "max_duration_seconds": args.max_duration,
        },
    }
    log_path = write_run_log(result, runs_dir=settings.runs_dir, payload=payload)

    report(result, log_path=log_path, quiet=args.quiet)

    output = json.dumps(result.to_dict(), indent=2, default=str)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
        if not args.quiet:
            print(f"   wrote       : {args.output}", file=sys.stderr)
    else:
        if not args.json_only:
            print("", file=sys.stderr)
        print(output)

    if result.succeeded:
        return EXIT_OK

    if verbose:
        print(
            f"\nFAILED: {result.diagnosis}\n"
            f"Full detail, including the raw engine response: {log_path}",
            file=sys.stderr,
        )
    return EXIT_RUN_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
