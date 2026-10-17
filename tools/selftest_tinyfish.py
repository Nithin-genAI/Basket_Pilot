"""Self-test: prove the TinyFish integration works, against a site that does not block.

This separates two failure modes that are otherwise easy to confuse:

  * "the TinyFish integration is broken" (bad key, bad schema, wrong endpoint)
  * "Blinkit blocked us" (the integration is fine; the target is hostile)

It runs a real TinyFish Agent API call against a neutral, scrapable demo shop and
asserts that structured output comes back. If this passes and Blinkit still fails, the
problem is Blinkit, not the integration.

    python tools/selftest_tinyfish.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from basketpilot.config import ConfigError, load_settings  # noqa: E402
from basketpilot.tinyfish_client import (  # noqa: E402
    TinyFishClient,
    build_output_schema,
    validate_schema,
)

TARGET = "https://scrapeme.live/shop"
GOAL = (
    "Extract the first 3 products listed on this page. "
    "For each, capture the exact product name as shown, and the price exactly as "
    "printed including the currency symbol. In the 'evidence' field, copy the verbatim "
    "on-screen text you read the name and price from. Report only what is visible. "
    "Return the results as JSON matching the provided schema."
)


def main() -> int:
    print("=== TinyFish self-test ===\n")

    settings = load_settings()
    if not settings.has_tinyfish_key:
        print("SKIP: no TINYFISH_API_KEY configured, so Engine A cannot be tested.")
        print("      Create .env with TINYFISH_API_KEY=sk-tinyfish-... and re-run.")
        return 2
    print(f"base url : {settings.tinyfish_base_url}")
    print(f"key      : {settings.tinyfish_api_key[:12]}...{settings.tinyfish_api_key[-4:]}")
    print()

    # Build and locally validate the same schema shape the real run uses.
    schema = build_output_schema(["pokemon"])
    try:
        validate_schema(schema)
        print(f"schema   : valid locally ({len(json.dumps(schema))} bytes)")
    except Exception as exc:  # noqa: BLE001
        print(f"schema   : INVALID locally — {exc}")
        return 3

    try:
        client = TinyFishClient(settings, verbose=True)
    except ConfigError as exc:
        print(f"\nconfig error: {exc}")
        return 2

    with client:
        print(f"\ntarget   : {TARGET}")
        print("starting a real run (this costs a little credit)...\n")

        run_id, start_body = client.start_run(
            url=TARGET,
            goal=GOAL,
            output_schema=schema,
            browser_profile="lite",  # a neutral demo site needs no stealth
            proxy_country=None,
            max_steps=60,
            max_duration_seconds=180,
        )

        if run_id is None:
            print("\nRESULT: FAILED at the start call.")
            print(f"  response: {json.dumps(start_body, indent=2, default=str)[:1500]}")
            code = (start_body.get("error") or {}).get("code")
            if code in ("MISSING_API_KEY", "INVALID_API_KEY", "UNAUTHORIZED"):
                print(f"\n  The API key itself was rejected ({code}). Check .env.")
            elif code == "INVALID_INPUT":
                print("\n  The request body was rejected — see details above.")
                print("  If it mentions output_schema, the local validator missed a keyword.")
            return 4

        final = client.poll_run(run_id, timeout_seconds=240)

    status = final.get("status")
    error = final.get("error") or {}
    result = final.get("result")

    print(f"\nrun_id   : {run_id}")
    print(f"status   : {status}")
    print(f"steps    : {final.get('num_of_steps')}")
    if error:
        print(f"error    : {error.get('code')} — {error.get('message')}")

    print("\nraw result:")
    print(json.dumps(result, indent=2, default=str)[:2000] if result else "  (none)")

    if status == "COMPLETED" and isinstance(result, dict) and result.get("products"):
        print("\nRESULT: PASS — the TinyFish integration works end to end.")
        print("        A Blinkit failure is therefore about Blinkit, not the plumbing.")
        return 0

    print("\nRESULT: the run did not return usable products.")
    print("        If the error above is a platform error (SERVICE_BUSY, TIMEOUT), retry.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
