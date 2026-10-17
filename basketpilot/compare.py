"""The end-to-end workflow: shopping list in, ranked basket recommendation out.

    python -m basketpilot compare --list "2L Amul milk, a dozen eggs, brown bread" \
        --pincode 560001

This is the Phase 2 deliverable. It parses the list, searches real retailers through
the configured engine, matches what comes back to the items requested, hands the
offers to the deterministic optimizer, and reports the recommendation along with the
evidence behind it.

Two reporting rules, both from the brief:

* **Progress goes to stderr, the payload to stdout.** ``--json-only`` must be
  pipe-clean, so nothing but JSON ever reaches stdout.
* **Failures are reported, not smoothed over.** A retailer that did not work appears
  in the output with its actual error, and the run's exit code reflects whether a
  basket was actually built.

No ordering, no cart, no payment. The workflow ends at a recommendation plus the
reasons for it, which is where the brief draws the safety boundary.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import adapters
from .agent import AgentRun, ShoppingAgent
from .config import Settings
from .models import RetailerResult
from .optimizer import Constraints

EXIT_OK = 0
EXIT_NO_BASKET = 1
EXIT_CONFIG_ERROR = 2


@dataclass
class CompareOptions:
    raw_list: str
    pincode: str = "560001"
    engine: str = "auto"
    retailers: list[str] | None = None
    max_platforms: int = 3
    budget: float | None = None
    split_penalty: float = 25.0
    country: str | None = "IN"
    # Optional warmed TinyFish Browser Context Profile. Recorded on the run so a result
    # can never be read as cold when it was warm, or the reverse.
    profile_id: str | None = None
    verbose: bool = True
    json_only: bool = False
    output: Path | None = None


def _progress(options: CompareOptions, message: str) -> None:
    if options.verbose:
        print(message, file=sys.stderr, flush=True)


def _retailer_line(key: str, result: RetailerResult) -> str:
    """One line summarising a retailer, honest about failure."""
    if result.succeeded:
        return f"  OK    {result.retailer:<18} {len(result.usable_products):>3} products via {result.engine}"
    reason = result.error_code or result.status
    detail = (result.error_message or "").strip().split(". ")[0][:110]
    return f"  FAIL  {result.retailer:<18} {reason}: {detail}"


def _report(run: AgentRun, options: CompareOptions) -> None:
    """Human-readable summary. stderr, always."""
    stream = sys.stderr
    print("", file=stream)
    print("── items ──", file=stream)
    for item in run.items:
        bits = [item.name]
        if item.brand:
            bits.append(f"brand={item.brand}{' (required)' if item.brand_required else ''}")
        if item.quantity is not None:
            bits.append(f"qty={item.quantity:g}{item.unit or ''}")
        print(f"  · {' | '.join(bits)}", file=stream)

    print("\n── retailers ──", file=stream)
    for key, result in run.results.items():
        print(_retailer_line(key, result), file=stream)

    print("\n── recommendation ──", file=stream)
    opt = run.optimization
    if opt is None or opt.recommended is None:
        print("  no basket could be built", file=stream)
        for note in (opt.notes if opt else []):
            print(f"  ! {note}", file=stream)
    else:
        best = opt.recommended
        print(
            f"  {', '.join(best.retailer_keys)}  ·  ₹{best.total:g}  ·  "
            f"{best.platform_count} platform(s)  ·  "
            f"{best.covered_items}/{best.total_items} items  ·  {best.cost_confidence}",
            file=stream,
        )
        for line in best.lines:
            if line.offer is not None:
                price = f"₹{line.offer.price:g}" if line.offer.price is not None else "price n/a"
                # The marker has to distinguish three states, not two. An item held for
                # approval has an offer attached but is NOT covered — printing it the
                # same way as a matched line would make a 250 ml carton standing in for
                # 2 L of milk look like a fulfilled line at a glance, which is exactly
                # the substitution this project refuses to make quietly.
                if line.status == "needs_approval":
                    marker = "[??]"
                else:
                    marker = f"[{line.offer.retailer}]"
                print(
                    f"    {marker} {line.offer.product.name}"
                    f"{' · ' + line.offer.product.package_size if line.offer.product.package_size else ''}"
                    f" — {price}",
                    file=stream,
                )
                if line.status == "needs_approval":
                    print(f"         not counted as covered: {line.reason}", file=stream)
            else:
                print(f"    [--] {line.item_name} — {line.reason}", file=stream)
        for reason in best.reasons:
            print(f"    · {reason}", file=stream)
        if best.unverified_charges:
            print(f"    ! unverified: {'; '.join(best.unverified_charges)}", file=stream)

    if run.notes:
        print("\n── notes ──", file=stream)
        for note in run.notes:
            print(f"  · {note}", file=stream)


def run_compare(options: CompareOptions, settings: Settings) -> tuple[AgentRun, int]:
    """Execute the workflow. Returns the run and the process exit code."""
    constraints = Constraints(
        max_platforms=options.max_platforms,
        budget=options.budget,
        split_penalty=options.split_penalty,
    )

    agent = ShoppingAgent(settings, verbose=options.verbose)

    _progress(
        options,
        f"[compare] list={options.raw_list!r} pincode={options.pincode} engine={options.engine}",
    )

    def on_result(key: str, result: RetailerResult) -> None:
        _progress(options, _retailer_line(key, result))

    run = agent.run(
        options.raw_list,
        pincode=options.pincode,
        engine=options.engine,
        retailers=options.retailers,
        constraints=constraints,
        screenshot_dir=settings.runs_dir,
        country=options.country,
        profile_id=options.profile_id,
        on_result=on_result,
    )

    if not run.items:
        return run, EXIT_CONFIG_ERROR
    if run.optimization is None or run.optimization.recommended is None:
        return run, EXIT_NO_BASKET
    return run, EXIT_OK


def write_run_log(run: AgentRun, options: CompareOptions, runs_dir: Path) -> Path:
    """Persist the whole run so any recommendation can be traced after the fact."""
    runs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = runs_dir / f"{stamp}-compare.json"
    path.write_text(
        json.dumps(
            {
                "request": {
                    "list": options.raw_list,
                    "pincode": options.pincode,
                    "engine": options.engine,
                    "max_platforms": options.max_platforms,
                    "budget": options.budget,
                    "split_penalty": options.split_penalty,
                    "country": options.country,
                    "profile_id": options.profile_id,
                },
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "result": run.to_dict(),
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    return path


def main(options: CompareOptions, settings: Settings) -> int:
    run, code = run_compare(options, settings)
    log_path = write_run_log(run, options, settings.runs_dir)

    if options.verbose:
        _report(run, options)
        print(f"\n  run log: {log_path}", file=sys.stderr)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "request": {
            "list": options.raw_list,
            "pincode": options.pincode,
            "engine": options.engine,
        },
        "result": run.to_dict(),
        "exit_code": code,
    }
    text = json.dumps(payload, indent=2, default=str)

    if options.output:
        options.output.parent.mkdir(parents=True, exist_ok=True)
        options.output.write_text(text, encoding="utf-8")
        if options.verbose:
            print(f"  wrote: {options.output}", file=sys.stderr)
    else:
        print(text)

    if code == EXIT_OK and options.verbose:
        print("\nNo order was placed and no payment was made — this is a recommendation only.", file=sys.stderr)
    return code


def retailer_report() -> dict[str, Any]:
    """The verification ledger, for the 'which retailers actually work' question."""
    adapters.load_ledger()
    return {
        key: {
            "name": adapters.get(key).name,
            **adapters.get(key).verification.to_dict(),
            "note": adapters.get(key).note,
        }
        for key in adapters.PRIORITY_ORDER
    }
