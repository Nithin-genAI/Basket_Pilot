"""BasketPilot Model Context Protocol (MCP) Server.

Exposes the BasketPilot comparison and basket optimization pipeline as an MCP tool
for consumption by local agents (e.g. TrueForge ChatUI).

Run with:
    python -m basketpilot.mcp_server --port 8766
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from basketpilot.compare import CompareOptions, run_compare
from basketpilot.config import load_settings
from basketpilot.optimizer import Basket

logger = logging.getLogger("basketpilot.mcp")

# Initialize FastMCP server
mcp = FastMCP(
    name="BasketPilot",
    instructions=(
        "BasketPilot connects to Indian quick-commerce retailers (Blinkit, Zepto, Instamart) "
        "via browser automation (TinyFish as primary execution layer) and computes the optimal "
        "grocery basket across stores with verified prices, delivery costs, and honest provenance."
    ),
)


def _format_basket_summary(run_dict: dict[str, Any], exit_code: int) -> str:
    """Produce a concise, clear markdown summary for the LLM to present to the user."""
    lines: list[str] = []
    opt = run_dict.get("optimization") or {}
    rec = opt.get("recommended")

    if not rec:
        lines.append("### ⚠️ No Complete Basket Found")
        for note in opt.get("notes", []):
            lines.append(f"- {note}")
        for note in run_dict.get("notes", []):
            lines.append(f"- {note}")
        return "\n".join(lines)

    total = rec.get("total", 0.0)
    platforms = ", ".join(rec.get("retailer_keys", []))
    covered = rec.get("covered_items", 0)
    total_items = rec.get("total_items", 0)
    confidence = rec.get("cost_confidence", "unverified")

    lines.append(f"### 🛒 Basket Recommendation: ₹{total:.2f}")
    lines.append(f"- **Stores**: {platforms} ({rec.get('platform_count', 1)} platform(s))")
    lines.append(f"- **Coverage**: {covered}/{total_items} items covered")
    lines.append(f"- **Cost Confidence**: `{confidence}`")
    lines.append("")
    lines.append("#### Items Breakdown:")

    for line in rec.get("lines", []):
        status = line.get("status")
        item_name = line.get("item_name")
        offer = line.get("offer")
        reason = line.get("reason", "")

        if offer:
            prod = offer.get("product", {})
            prod_name = prod.get("name", item_name)
            pkg = f" ({prod.get('package_size')})" if prod.get("package_size") else ""
            price = f"₹{offer.get('price'):.2f}" if offer.get("price") is not None else "N/A"
            retailer = offer.get("retailer", "unknown")
            engine = offer.get("engine", "unknown")

            if status == "needs_approval":
                lines.append(f"- ⚠️ **[NEEDS APPROVAL]** {item_name} → {prod_name}{pkg} — {price} at {retailer} `[Engine: {engine}]`")
                lines.append(f"  *Reason*: {reason}")
            else:
                lines.append(f"- ✅ **{item_name}**: {prod_name}{pkg} — {price} at {retailer} `[Engine: {engine}]`")
        else:
            lines.append(f"- ❌ **{item_name}**: Unavailable ({reason})")

    unverified = rec.get("unverified_charges", [])
    if unverified:
        lines.append("")
        lines.append("#### Additional Charges / Uncertainties:")
        for charge in unverified:
            lines.append(f"- ℹ️ {charge}")

    lines.append("")
    lines.append("#### Retailer Execution Status:")
    for ret_key, ret_info in run_dict.get("retailers", {}).items():
        engine_used = ret_info.get("engine", "unknown")
        status_text = "OK" if ret_info.get("succeeded") else "FAILED"
        prod_count = ret_info.get("product_count", 0)
        lines.append(f"- **{ret_key}**: `{status_text}` ({prod_count} products via `{engine_used}`)")

    lines.append("")
    lines.append("> **Note**: No order was placed and no payment was made. This is an audited price and basket recommendation only.")
    return "\n".join(lines)


@mcp.tool(
    name="compare_grocery_basket",
    description=(
        "Search Indian quick-commerce retailers (e.g. Blinkit) for a shopping list, "
        "retrieve real-time product prices and availability via browser automation, "
        "and calculate the optimal grocery basket. Preserves execution provenance (TinyFish vs fallback)."
    ),
)
def compare_grocery_basket(
    shopping_list: str,
    pincode: str = "560001",
    retailers: list[str] | None = None,
    engine: str = "auto",
    profile_id: str | None = None,
) -> dict[str, Any]:
    """Execute grocery search and basket optimization across quick-commerce stores.

    Args:
        shopping_list: Plain text list of items (e.g. "2L Amul milk, a dozen eggs, brown bread").
        pincode: 6-digit Indian delivery pincode (default "560001"). Quick-commerce catalogs are pincode-gated.
        retailers: Optional list of retailers to search (e.g. ["blinkit"]). Default searches verified retailers.
        engine: Browser execution engine ("auto", "tinyfish_browser", or "cloak"). Defaults to "auto" (TinyFish first, with labelled fallback).
        profile_id: Optional warmed TinyFish browser profile ID (e.g. "prof_49f953ae3ed84aba").
    """
    settings = load_settings()

    # If no profile_id explicitly passed, use the blinkit-560001 profile if available
    effective_profile = profile_id or os.environ.get("TINYFISH_PROFILE_ID") or "prof_49f953ae3ed84aba"

    options = CompareOptions(
        raw_list=shopping_list,
        pincode=pincode,
        engine=engine,
        retailers=retailers,
        profile_id=effective_profile,
        verbose=True,
    )

    run, exit_code = run_compare(options, settings)
    run_dict = run.to_dict()

    summary = _format_basket_summary(run_dict, exit_code)

    return {
        "status": "success" if exit_code == 0 else "partial_or_failed",
        "exit_code": exit_code,
        "summary": summary,
        "recommended_basket": run_dict.get("optimization", {}).get("recommended") if run_dict.get("optimization") else None,
        "items": run_dict.get("items", []),
        "retailers": run_dict.get("retailers", {}),
        "notes": run_dict.get("notes", []),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="BasketPilot MCP Server")
    parser.add_argument("--port", type=int, default=8766, help="Port to listen on (default: 8766)")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind to (default: 127.0.0.1)")
    parser.add_argument(
        "--transport",
        choices=["sse", "streamable-http", "http", "stdio"],
        default="sse",
        help="Transport type (default: sse)",
    )
    args = parser.parse_args()

    print(f"Starting BasketPilot MCP server on {args.host}:{args.port} via {args.transport}...", file=sys.stderr)
    mcp.run(transport=args.transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
