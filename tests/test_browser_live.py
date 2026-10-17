"""Live-browser integration tests. Skipped unless ``-m browser`` is passed.

These are the only tests here that touch the network, cost TinyFish credit, and can
fail because a real website changed. That is the point: the offline suite proves the
*logic* is right, and these prove the *integration* still reaches a real catalogue.

Run them deliberately, not by accident:

    pytest -m browser                 # every accessible retailer
    pytest -m browser -k blinkit      # one retailer

Nothing here asserts a specific price or product name. Prices move, stock rotates, and
a test that pins "Amul Taaza 500 ml costs ₹28" would fail for reasons that have nothing
to do with whether the pipeline works. What is asserted is the thing that actually
matters and would actually break: that a real browser run produces validated products,
that each one carries a live source and the engine that produced it, and that a retailer
is only recorded as verified once that has happened.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from basketpilot import adapters  # noqa: E402
from basketpilot.config import load_settings  # noqa: E402
from basketpilot.listparse import parse_list  # noqa: E402

pytestmark = pytest.mark.browser

# One small, stable, genuinely stocked query. Not a full basket: this is an
# integration test for the *transport*, and the optimizer is covered offline.
LIVE_LIST = "milk"


def _settings():
    settings = load_settings()
    if not settings.has_tinyfish_key:
        pytest.skip("TINYFISH_API_KEY is not set")
    return settings


def _engine() -> str:
    return os.environ.get("BASKETPILOT_LIVE_ENGINE", adapters.ENGINE_TINYFISH_BROWSER)


@pytest.mark.parametrize("key", list(adapters.PRIORITY_ORDER))
def test_retailer_produces_real_products(key: str):
    """Each retailer either yields real products, or fails with a named blocker.

    A skip is not a pass: a retailer that cannot be reached is reported as a documented
    blocker rather than quietly omitted, which is what the brief requires.
    """
    settings = _settings()
    retailer = adapters.get(key)
    items = parse_list(LIVE_LIST)

    result = adapters.search(
        retailer,
        items=items,
        pincode="560001",
        engine=_engine(),
        settings=settings,
        verbose=True,
    )

    if not result.succeeded:
        pytest.skip(
            f"{retailer.name} did not return products via {_engine()}: "
            f"{result.error_code or result.status} — {result.error_message or ''}".strip()
        )

    products = result.usable_products
    assert products, f"{retailer.name} reported success with no usable products"

    for product in products:
        assert product.name, "a product with no name passed validation"
        # Every row must say where it came from and which engine produced it. A row
        # that cannot name its engine cannot be told apart from a fixture.
        assert product.source, f"{product.name!r} has no source"
        # The retailer lives on the *result*, not the product — Product is the Phase 1
        # record and carries no retailer field. Asserting it here was a bug in this
        # test, caught by the test itself failing rather than by the code being wrong.
        assert result.retailer == retailer.name
        assert result.engine == _engine()


def test_a_successful_run_records_verification():
    """The ledger rule: 'working' is written by a real run, not by hand."""
    settings = _settings()
    retailer = adapters.get("blinkit")
    items = parse_list(LIVE_LIST)

    result = adapters.search(
        retailer, items=items, pincode="560001", engine=_engine(), settings=settings, verbose=True
    )
    if not result.succeeded:
        pytest.skip(f"blinkit unavailable this run: {result.error_code or result.status}")

    assert retailer.verification.is_verified
    assert retailer.verification.products_seen > 0
    assert retailer.verification.verified_engine == result.engine
    assert "blinkit" in adapters.verified_keys()
