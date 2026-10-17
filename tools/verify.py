"""Offline verification of BasketPilot's invariants. No network, no API key.

These are the claims the project makes about itself, each checked rather than asserted.
They are the checks that would catch a regression in the parts that matter: the
anti-fabrication gate, query attribution, and the rule that an unconfirmed delivery
location is a failure rather than a quiet success.

    python tools/verify.py

Exits 0 only if every check passes.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from basketpilot.cloak_client import (  # noqa: E402
    _recorder_emitter,
    _screenshot_named,
    _url_names_query,
)
from basketpilot.models import Product, RetailerResult, parse_price_displayed  # noqa: E402
from basketpilot.normalize import dedupe, merge  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(label)
        print(f"  PASS  {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL  {label}" + (f"\n        {detail}" if detail else ""))


def gate(label: str, product: Product, expect_price: float | None) -> None:
    """Validate a product and assert the resulting numeric price."""
    product.validate()
    check(
        label,
        product.price_numeric == expect_price,
        f"price_numeric={product.price_numeric!r}, expected {expect_price!r}; "
        f"warnings={product.warnings}",
    )


def main() -> int:
    print("\n=== anti-fabrication gate ===")

    # A number with no readable price behind it must be dropped, not trusted.
    gate(
        "a fabricated numeric with no displayed price is dropped",
        Product(query="milk", name="X", price_displayed="MRP", price_numeric=999.0),
        None,
    )
    # The numeric must be derived from the displayed string.
    gate(
        "the numeric price is derived from the displayed string",
        Product(query="milk", name="Amul Taaza", price_displayed="₹28"),
        28.0,
    )
    # Evidence that does not contain the price must kill the price.
    gate(
        "a price its evidence does not corroborate is dropped",
        Product(query="milk", name="X", price_displayed="₹99", evidence="X 1 L"),
        None,
    )
    gate(
        "a price its evidence does corroborate is kept",
        Product(query="milk", name="X", price_displayed="₹99", evidence="X 1 L ₹99"),
        99.0,
    )
    # A pack size must never be read as a price.
    gate(
        "a pack size is not read as a price",
        Product(query="milk", name="X", price_displayed="500 ml"),
        None,
    )
    gate(
        "a thousands separator parses correctly",
        Product(query="oil", name="X", price_displayed="₹1,299", evidence="X ₹1,299"),
        1299.0,
    )
    check(
        "a bare number with no currency marker is not a price",
        parse_price_displayed("28") is None,
    )

    # Provenance warnings must survive validation, or a validated row would be less
    # auditable than an unvalidated one.
    provenance = Product(query="milk", name="X", price_displayed="₹10", evidence="₹10")
    provenance.warnings.append("blinkit product_id=477483")
    provenance.validate()
    check(
        "extractor provenance survives validation",
        "blinkit product_id=477483" in provenance.warnings and provenance.price_numeric == 10.0,
        f"warnings={provenance.warnings}",
    )

    print("\n=== what counts as a usable result ===")
    name_only = Product(query="milk", name="Skeleton")
    name_only.validate()
    check("a name alone is not usable", not name_only.is_usable)

    out_of_stock = Product(query="milk", name="X", availability="out_of_stock")
    out_of_stock.validate()
    check("an out-of-stock row is usable", out_of_stock.is_usable)

    completed_empty = RetailerResult(
        retailer="Blinkit", engine="tinyfish", queries=["milk"], status="COMPLETED"
    )
    check(
        "COMPLETED with no products is not success",
        not completed_empty.succeeded,
        f"diagnosis={completed_empty.diagnosis!r}",
    )

    print("\n=== query attribution ===")
    # The bug this guards: a "bread" search returning the milk payload, because the milk
    # response happens to mention bread in a category name.
    cases = [
        ("https://blinkit.com/v1/layout/search?q=milk&search_type=type_to_search", "milk", True),
        ("https://blinkit.com/v1/layout/search?q=milk&search_type=type_to_search", "bread", False),
        ("https://blinkit.com/v1/layout/search?actual_query=bread&q=bread", "bread", True),
        ("https://blinkit.com/v1/layout/search?q=eggs", "eggs", True),
        ("https://blinkit.com/v1/layout/search?q=eggs", "milk", False),
    ]
    for url, query, expected in cases:
        check(
            f"url naming {query!r} -> {expected}",
            _url_names_query(url, query) is expected,
            f"url={url}",
        )

    print("\n=== an unconfirmed delivery location is a failure ===")
    wrong_place = RetailerResult(
        retailer="Blinkit",
        engine="cloak",
        queries=["milk"],
        status="FAILED",
        delivery_pincode="744101",
        error_code="LOCATION_NOT_SET",
        products=[Product(query="milk", name="Amul Taaza", price_displayed="₹30")],
    )
    check(
        "products for an unknown location do not count as success",
        not wrong_place.succeeded,
        f"succeeded={wrong_place.succeeded}",
    )

    print("\n=== fallback keeps the primary engine's diagnostics ===")
    primary = RetailerResult(
        retailer="Blinkit",
        engine="tinyfish",
        queries=["milk"],
        status="FAILED",
        error_code="LOCATION_NOT_SET",
        run_id="f67de97c",
        num_of_steps=7,
        warnings=["agent note: the location popup kept reappearing"],
        raw={
            "final_response": {
                "result": {"products": [], "location_set": False, "notes": "popup would not close"}
            }
        },
    )
    rescue = RetailerResult(
        retailer="Blinkit", engine="cloak", queries=["milk"], status="COMPLETED"
    )
    merged = merge(primary, rescue)
    failure = merged.raw["primary_engine_failure"]
    check("the rescue's data is used", merged.engine == "cloak")
    check("the primary's run id is kept", failure["run_id"] == "f67de97c")
    check("the primary's step count is kept", failure["num_of_steps"] == 7)
    check(
        "the agent's own explanation is kept",
        failure.get("agent_result", {}).get("notes") == "popup would not close",
        f"agent_result={failure.get('agent_result')!r}",
    )
    check(
        "the fallback is announced in the warnings",
        any("tinyfish failed" in w for w in merged.warnings),
        f"warnings={merged.warnings}",
    )

    print("\n=== dedupe ===")
    duplicates = [
        Product(query="milk", name="Amul Taaza", package_size="500 ml"),
        Product(query="milk", name="amul taaza", package_size="500 ML"),
        Product(query="milk", name="Amul Gold", package_size="1 ltr"),
    ]
    check("case-insensitive duplicates collapse", len(dedupe(duplicates)) == 2)

    print("\n=== empty-query screenshots are safe and distinct ===")
    import tempfile

    class _FakePage:
        def __init__(self, fail: bool = False) -> None:
            self.fail = fail

        def screenshot(self, path: str) -> None:
            if self.fail:
                raise RuntimeError("browser closed")
            Path(path).write_bytes(b"png")

    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        first = _screenshot_named(_FakePage(), directory, "Blinkit-milk-empty", lambda _: None)
        second = _screenshot_named(_FakePage(), directory, "Blinkit-bread-empty", lambda _: None)
        check("two empty queries do not overwrite each other", first != second and first is not None)
        check(
            "a failing screenshot returns None instead of raising",
            _screenshot_named(_FakePage(fail=True), directory, "x", lambda _: None) is None,
        )
        check(
            "no screenshot directory is a no-op",
            _screenshot_named(_FakePage(), None, "x", lambda _: None) is None,
        )

    print("\n=== the network recorder listens on the context, not the page ===")
    # The bug this guards: with the recorder on the page, reading a response body inside
    # the handler re-enters the sync API during `page.emit(...)`, which is the same
    # dispatch that drives `expect_response`. The waiter tears itself down mid-iteration
    # and pyee's naked `list.pop` raises KeyError. On the context the read happens in a
    # separate, earlier emit and the collision cannot occur.
    context = object()
    page_with_context = type("P", (), {"context": context})()
    check(
        "the owning context is preferred over the page",
        _recorder_emitter(page_with_context, lambda _: None) is context,
    )

    class _ContextRaises:
        @property
        def context(self) -> object:
            raise RuntimeError("not supported by this build")

    fallback = _ContextRaises()
    logged: list[str] = []
    check(
        "a page whose context is unavailable falls back to the page, not an exception",
        _recorder_emitter(fallback, logged.append) is fallback and bool(logged),
        f"logged={logged}",
    )
    check(
        "an explicit null context falls back to the page and says so",
        _recorder_emitter(type("P", (), {"context": None})(), logged.append) is not None,
    )

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("\nFAILED CHECKS:")
        for label in FAILED:
            print(f"  - {label}")
        return 1
    print("\nAll invariants hold.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
