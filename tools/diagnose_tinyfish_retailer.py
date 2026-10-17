"""Diagnostic: can the TinyFish agent do *any* Indian grocery retailer?

Blinkit left a TinyFish run `RUNNING` for 20+ minutes without reaching a terminal
status. That single data point is ambiguous — it could mean:

  (a) TinyFish cannot drive Indian quick-commerce sites at all, or
  (b) Blinkit specifically is too hard (Cloudflare + a location gate + a heavy SPA).

This separates the two by pointing the same agent, with the same schema and the same
goal shape, at a softer Indian grocery retailer that answered plain HTTP 200. If this
completes, the problem is Blinkit; if it also hangs, the problem is the platform's
reach into Indian retail.

This is a **diagnostic, not a retailer adapter.** It adds no retailer to the product,
builds no second integration, and its output is not part of the CLI's normal path.

    python tools/diagnose_tinyfish_retailer.py --url https://www.jiomart.com/ --label JioMart
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from basketpilot.config import load_settings  # noqa: E402
from basketpilot.tinyfish_client import (  # noqa: E402
    TinyFishClient,
    build_output_schema,
)

GOAL = (
    "Search this grocery site for {query}. From the results page, report at most 3 "
    "products. For each, capture the exact product name as shown, the brand, the "
    "package size, the price exactly as printed including the currency symbol, whether "
    "it is in stock, and the product URL. In 'evidence', copy the verbatim on-screen "
    "text you read the price from. Report only what is visible; return null for any "
    "field that is not shown, and never estimate or recall a price from memory. "
    "Return the results as JSON matching the provided schema."
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True, help="Retailer root URL.")
    parser.add_argument("--label", default="retailer", help="Name for the log line.")
    parser.add_argument("--query", default="milk")
    parser.add_argument("--pincode", default="560001")
    parser.add_argument(
        "--max-wait", type=int, default=600, help="Seconds to poll before giving up."
    )
    args = parser.parse_args()

    settings = load_settings()
    if not settings.has_tinyfish_key:
        print("SKIP: no TINYFISH_API_KEY configured.")
        return 2

    schema = build_output_schema([args.query])
    client = TinyFishClient(settings, verbose=True)

    with client:
        print(f"\ntarget : {args.label} ({args.url})")
        print(f"query  : {args.query}   pincode: {args.pincode}\n")

        run_id, start_body = client.start_run(
            url=args.url,
            goal=GOAL.format(query=args.query),
            output_schema=schema,
            browser_profile="stealth",
            proxy_country="IN",
            max_steps=250,
            max_duration_seconds=args.max_wait,
        )
        if run_id is None:
            print("\nRESULT: FAILED to start.")
            print(json.dumps(start_body, indent=2, default=str)[:1200])
            return 1

        final = client.poll_run(run_id, timeout_seconds=args.max_wait)

    status = final.get("status")
    error = final.get("error") or {}
    result = final.get("result")
    products = (result or {}).get("products") if isinstance(result, dict) else None

    print(f"\nrun_id  : {run_id}")
    print(f"status  : {status}")
    print(f"steps   : {final.get('num_of_steps')}")
    if error:
        print(f"error   : {error.get('code')} — {error.get('message')}")
    if products:
        print(f"\n{len(products)} product(s):")
        for product in products[:5]:
            print(
                f"  - {product.get('name')} | {product.get('package_size')} | "
                f"{product.get('price_displayed')} | {product.get('availability')}"
            )
    else:
        print("\nno products returned")
        print(json.dumps(result, indent=2, default=str)[:900] if result else "")

    out = Path("runs") / f"diagnose-{args.label.lower()}-tinyfish.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"run_id": run_id, "final": final}, indent=2, default=str))
    print(f"\nwritten -> {out}")

    if status == "COMPLETED" and products:
        print(
            f"\nRESULT: TinyFish completed a real task on {args.label}. "
            "The Blinkit failure is therefore about Blinkit, not about Indian retail."
        )
        return 0
    print(
        f"\nRESULT: TinyFish did not complete {args.label} either "
        f"(status={status}). See {out}."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
