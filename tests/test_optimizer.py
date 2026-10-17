"""Unit tests for the deterministic optimizer. No network, no API key, no browser.

These are the claims the optimizer makes about itself, each checked against a
synthetic fixture. The prices are invented; the arithmetic is real.
"""

from __future__ import annotations

import pytest

from basketpilot.optimizer import (
    CONFIDENCE_ESTIMATED,
    CONFIDENCE_VERIFIED,
    LINE_UNAVAILABLE,
    Constraints,
    build_basket,
    optimize,
)

from conftest import make_item, make_offer, make_quote


# --------------------------------------------------------------------------- #
# Coverage ranks above price
# --------------------------------------------------------------------------- #


def test_complete_basket_beats_a_cheaper_incomplete_one():
    """A cheaper basket missing an item must not win. This is the top-level rule."""
    milk = make_item("milk")
    bread = make_item("bread")
    offers = [
        # cheap retailer: milk only, very cheap
        make_offer("cheap", milk, name="Cheap Milk", price=5.0, pack_size="500 ml"),
        # dearer retailer: covers both
        make_offer("dear", milk, name="Dear Milk", price=40.0, pack_size="500 ml"),
        make_offer("dear", bread, name="Dear Bread", price=40.0, pack_size="400 g"),
    ]
    quotes = {
        "cheap": make_quote("cheap", delivery_fee=0.0, delivery_fee_verified=True),
        "dear": make_quote("dear", delivery_fee=0.0, delivery_fee_verified=True),
    }

    result = optimize([milk, bread], offers, quotes)

    assert result.recommended is not None
    assert result.recommended.is_complete
    assert "dear" in result.recommended.retailer_keys
    assert result.recommended.covered_items == 2


def test_incomplete_basket_is_reported_with_its_missing_items():
    milk = make_item("milk")
    bread = make_item("bread")
    offers = [make_offer("only", milk, name="Only Milk", price=30.0, pack_size="500 ml")]

    result = optimize([milk, bread], offers, {})

    assert result.recommended is not None
    assert not result.recommended.is_complete
    assert result.unavailable == ["bread"]


def test_nothing_usable_yields_no_recommendation():
    milk = make_item("milk")
    offers = [
        make_offer("a", milk, name="Unpriced Milk", price=None, pack_size="500 ml"),
        make_offer("b", milk, name="Out of Stock Milk", price=30.0,
                   pack_size="500 ml", availability="out_of_stock"),
    ]

    result = optimize([milk], offers, {})

    assert result.recommended is None
    assert result.unavailable == ["milk"]


# --------------------------------------------------------------------------- #
# Splitting
# --------------------------------------------------------------------------- #


def test_split_is_penalised_and_the_single_platform_wins(two_retailers):
    """beta is ₹3 cheaper on milk, but its ₹40 delivery loses to alpha's completeness."""
    result = optimize(
        two_retailers["items"], two_retailers["offers"], two_retailers["quotes"]
    )
    best = result.recommended

    assert best is not None
    assert best.platform_count == 1
    assert best.retailer_keys == ("alpha",)
    assert best.is_complete
    # 30 + 45 + 70, alpha delivers free
    assert best.subtotal == 145.0
    assert best.fees == 0.0
    assert best.total == 145.0


def test_split_penalty_is_what_stops_a_trivial_split():
    """Without a split penalty a ₹1 saving would justify a second platform."""
    milk = make_item("milk")
    bread = make_item("bread")
    offers = [
        # a covers both; b is a rupee cheaper on milk alone, so using it means a split.
        make_offer("a", milk, name="A Milk", price=30.0, pack_size="500 ml"),
        make_offer("a", bread, name="A Bread", price=30.0, pack_size="400 g"),
        make_offer("b", milk, name="B Milk", price=29.0, pack_size="500 ml"),
    ]
    quotes = {
        "a": make_quote("a", delivery_fee=0.0, delivery_fee_verified=True),
        "b": make_quote("b", delivery_fee=0.0, delivery_fee_verified=True),
    }

    penalised = optimize([milk, bread], offers, quotes,
                         Constraints(split_penalty=25.0))
    assert penalised.recommended.platform_count == 1

    # With no penalty the ₹1 saving wins, which is exactly the behaviour the penalty
    # exists to prevent.
    unpenalised = optimize([milk, bread], offers, quotes,
                           Constraints(split_penalty=0.0))
    assert unpenalised.recommended.platform_count == 2


def test_a_split_is_chosen_when_it_is_genuinely_worth_it():
    """The penalty must not be so blunt that it forbids a large real saving."""
    milk = make_item("milk")
    bread = make_item("bread")
    offers = [
        make_offer("a", milk, name="A Milk", price=100.0, pack_size="500 ml"),
        make_offer("a", bread, name="A Bread", price=100.0, pack_size="400 g"),
        make_offer("b", milk, name="B Milk", price=20.0, pack_size="500 ml"),
        make_offer("b", bread, name="B Bread", price=20.0, pack_size="400 g"),
    ]
    quotes = {
        "a": make_quote("a", delivery_fee=0.0, delivery_fee_verified=True),
        "b": make_quote("b", delivery_fee=0.0, delivery_fee_verified=True),
    }

    result = optimize([milk, bread], offers, quotes, Constraints(split_penalty=25.0))

    # b alone is complete at 40 — no split needed, and the optimizer should see that.
    assert result.recommended.retailer_keys == ("b",)
    assert result.recommended.total == 40.0


def test_platform_count_is_capped_by_max_platforms():
    items = [make_item(n) for n in ("milk", "bread", "eggs")]
    offers = []
    for retailer in ("a", "b", "c"):
        for item in items:
            offers.append(
                make_offer(retailer, item, name=f"{retailer} {item.name}",
                           price=30.0, pack_size="500 ml")
            )
    quotes = {r: make_quote(r, delivery_fee=0.0, delivery_fee_verified=True)
              for r in ("a", "b", "c")}

    result = optimize(items, offers, quotes, Constraints(max_platforms=1))

    assert all(b.platform_count <= 1 for b in [result.recommended, *result.alternatives])


# --------------------------------------------------------------------------- #
# Missing values are never zero
# --------------------------------------------------------------------------- #


def test_unverified_delivery_fee_is_not_counted_as_free():
    """The central 'missing is not zero' case."""
    milk = make_item("milk")
    offers = [make_offer("a", milk, name="A Milk", price=30.0, pack_size="500 ml")]
    quotes = {"a": make_quote("a")}  # delivery_fee is None, and unverified

    result = optimize([milk], offers, quotes)
    best = result.recommended

    assert best.subtotal == 30.0
    # The fee is not added, because we cannot prove it exists...
    assert best.fees == 0.0
    # ...but the basket is not presented as a verified total either.
    assert best.cost_confidence == CONFIDENCE_ESTIMATED
    assert any("delivery fee not verified" in c for c in best.unverified_charges)


def test_verified_fees_make_a_basket_verified():
    milk = make_item("milk")
    offers = [make_offer("a", milk, name="A Milk", price=30.0, pack_size="500 ml")]
    quotes = {
        "a": make_quote(
            "a", delivery_fee=25.0, delivery_fee_verified=True,
            handling_fee=10.0, handling_fee_verified=True,
            small_cart_fee=0.0, small_cart_fee_verified=True,
            discount=5.0, discount_verified=True,
        )
    }

    result = optimize([milk], offers, quotes)
    best = result.recommended

    assert best.cost_confidence == CONFIDENCE_VERIFIED
    assert best.unverified_charges == []
    assert best.fees == 35.0
    assert best.discounts == 5.0
    assert best.total == 30.0 + 35.0 - 5.0


def test_free_delivery_threshold_is_honoured():
    milk = make_item("milk")
    offers = [make_offer("a", milk, name="A Milk", price=200.0, pack_size="500 ml")]
    quotes = {
        "a": make_quote("a", delivery_fee=30.0, delivery_fee_verified=True,
                        free_delivery_threshold=150.0)
    }

    result = optimize([milk], offers, quotes)

    assert result.recommended.fees == 0.0
    assert result.recommended.total == 200.0


def test_unverified_discount_is_not_applied():
    milk = make_item("milk")
    offers = [make_offer("a", milk, name="A Milk", price=100.0, pack_size="500 ml")]
    quotes = {"a": make_quote("a", discount=50.0, discount_verified=False)}

    result = optimize([milk], offers, quotes)

    assert result.recommended.discounts == 0.0
    assert result.recommended.total == 100.0
    assert any("discounts not verified" in c for c in result.recommended.unverified_charges)


def test_a_product_with_no_readable_price_is_excluded_not_zeroed():
    milk = make_item("milk")
    offers = [
        make_offer("a", milk, name="Free Looking Milk", price=None, pack_size="500 ml"),
        make_offer("b", milk, name="Real Milk", price=30.0, pack_size="500 ml"),
    ]
    quotes = {"b": make_quote("b", delivery_fee=0.0, delivery_fee_verified=True)}

    result = optimize([milk], offers, quotes)

    assert result.recommended.retailer_keys == ("b",)
    assert result.recommended.total == 30.0


# --------------------------------------------------------------------------- #
# Constraints
# --------------------------------------------------------------------------- #


def test_a_required_brand_is_not_substituted():
    """A cheaper different brand must not be bought when the user named a brand."""
    milk = make_item("milk", brand="Amul", brand_required=True)
    offers = [
        make_offer("a", milk, name="Amul Taaza Milk", price=30.0,
                   pack_size="500 ml", brand="Amul"),
        # Cheaper, and deliberately given a *high* confidence, so the only thing that
        # can exclude it is the brand constraint itself.
        make_offer("b", milk, name="Heritage Milk", price=20.0,
                   pack_size="500 ml", brand="Heritage", confidence=0.9),
    ]
    quotes = {
        "a": make_quote("a", delivery_fee=0.0, delivery_fee_verified=True),
        "b": make_quote("b", delivery_fee=0.0, delivery_fee_verified=True),
    }

    result = optimize([milk], offers, quotes)

    assert result.recommended.retailer_keys == ("a",)
    assert result.recommended.total == 30.0


def test_the_matcher_itself_rejects_a_wrong_brand():
    """The same rule, at the layer that builds offers rather than the one that spends."""
    from basketpilot.matching import match

    item = make_item("milk", brand="Amul", brand_required=True)
    wrong = make_offer("b", item, name="Heritage Milk", price=20.0,
                       pack_size="500 ml", brand="Heritage")
    right = make_offer("a", item, name="Amul Taaza Milk", price=30.0,
                       pack_size="500 ml", brand="Amul")

    assert not match(wrong.product, item).accepted
    assert match(right.product, item).accepted


def test_a_wrong_size_pack_is_held_for_approval_not_silently_bought():
    """The bug this guards: '2L milk' fulfilled by a 250 ml carton because it was cheaper."""
    milk = make_item("milk", quantity=2000.0, unit="ml")
    small = make_offer("a", milk, name="Amul Milk", price=26.0,
                       pack_size="250 ml", brand="Amul")
    small.pack_mismatch = True
    small.pack_note = "pack is 250 ml but the request was 2000ml"

    result = optimize([milk], [small], {"a": make_quote("a", delivery_fee=0.0, delivery_fee_verified=True)})
    best = result.recommended

    assert best is not None
    assert best.covered_items == 0
    assert best.needs_approval == ["milk"]
    assert not best.is_complete


def test_a_correctly_sized_pack_wins_over_a_cheaper_wrong_one():
    """Pack size must rank ahead of price, or the cheaper wrong pack always wins."""
    milk = make_item("milk", quantity=2000.0, unit="ml")
    small = make_offer("a", milk, name="Amul Milk", price=26.0,
                       pack_size="250 ml", brand="Amul")
    small.pack_mismatch = True
    big = make_offer("a", milk, name="Amul Milk", price=120.0,
                     pack_size="2 L", brand="Amul")

    result = optimize([milk], [small, big],
                      {"a": make_quote("a", delivery_fee=0.0, delivery_fee_verified=True)})
    best = result.recommended

    assert best.covered_items == 1
    assert best.total == 120.0
    assert best.lines[0].offer.pack_quantity == 2000.0


def test_a_wrong_pack_is_allowed_when_substitutions_are_permitted():
    milk = make_item("milk", quantity=2000.0, unit="ml")
    small = make_offer("a", milk, name="Amul Milk", price=26.0,
                       pack_size="250 ml", brand="Amul")
    small.pack_mismatch = True

    result = optimize(
        [milk], [small],
        {"a": make_quote("a", delivery_fee=0.0, delivery_fee_verified=True)},
        Constraints(allow_substitutions=True),
    )

    assert result.recommended.covered_items == 1


def test_pack_size_matching_in_the_matcher_itself():
    from basketpilot.matching import match

    item = make_item("milk", quantity=2000.0, unit="ml")
    big = make_offer("a", item, name="Amul Milk", price=120.0, pack_size="2 L", brand="Amul")
    small = make_offer("a", item, name="Amul Milk", price=26.0, pack_size="250 ml", brand="Amul")

    assert match(big.product, item).pack_mismatch is False
    assert match(small.product, item).pack_mismatch is True


def test_an_unreadable_pack_size_is_flagged_not_assumed():
    """A pack whose size cannot be read is not silently treated as the right size."""
    from basketpilot.matching import match

    item = make_item("milk", quantity=2000.0, unit="ml")
    mystery = make_offer("a", item, name="Amul Milk", price=120.0, pack_size=None, brand="Amul")

    verdict = match(mystery.product, item)

    assert verdict.pack_mismatch is True
    assert "could not be read" in verdict.pack_note


def test_no_requested_size_means_any_pack_fits():
    from basketpilot.matching import match

    item = make_item("milk")  # no quantity
    any_pack = make_offer("a", item, name="Amul Milk", price=26.0,
                          pack_size="250 ml", brand="Amul")

    assert match(any_pack.product, item).pack_mismatch is False


def test_low_confidence_offers_are_rejected():
    milk = make_item("milk")
    offers = [
        make_offer("a", milk, name="Barely Related", price=10.0,
                   pack_size="500 ml", confidence=0.2),
    ]

    result = optimize([milk], offers, {})

    assert result.recommended is None
    assert result.unavailable == ["milk"]


def test_over_budget_is_flagged_not_hidden():
    milk = make_item("milk")
    offers = [make_offer("a", milk, name="A Milk", price=500.0, pack_size="500 ml")]
    quotes = {"a": make_quote("a", delivery_fee=0.0, delivery_fee_verified=True)}

    result = optimize([milk], offers, quotes, Constraints(budget=100.0))

    assert result.recommended.over_budget
    assert any("budget" in w for w in result.recommended.warnings)


# --------------------------------------------------------------------------- #
# The verification gate
# --------------------------------------------------------------------------- #


def test_unverified_retailers_cannot_form_a_basket():
    """The rule 'never mark a retailer working until a real run succeeded', in code."""
    milk = make_item("milk")
    offers = [make_offer("unproven", milk, name="Unproven Milk", price=10.0, pack_size="500 ml")]

    gated = optimize([milk], offers, {}, verified_retailers=["blinkit"])
    assert gated.recommended is None
    assert any("no verified live run" in n for n in gated.notes)

    ungated = optimize([milk], offers, {})
    assert ungated.recommended is not None


# --------------------------------------------------------------------------- #
# Explanation
# --------------------------------------------------------------------------- #


def test_reasons_explain_a_single_platform_choice(two_retailers):
    result = optimize(
        two_retailers["items"], two_retailers["offers"], two_retailers["quotes"]
    )
    reasons = " ".join(result.recommended.reasons)

    assert "covers all 3 items" in reasons


def test_reasons_name_the_unverified_charge():
    milk = make_item("milk")
    offers = [make_offer("a", milk, name="A Milk", price=30.0, pack_size="500 ml")]

    result = optimize([milk], offers, {"a": make_quote("a")})

    assert "estimate" in " ".join(result.recommended.reasons)


def test_alternatives_are_offered_and_capped():
    items = [make_item(n) for n in ("milk", "bread", "eggs")]
    offers = []
    for retailer, price in (("a", 30.0), ("b", 31.0), ("c", 32.0), ("d", 33.0), ("e", 34.0)):
        for item in items:
            offers.append(
                make_offer(retailer, item, name=f"{retailer} {item.name}",
                           price=price, pack_size="500 ml")
            )
    quotes = {r: make_quote(r, delivery_fee=0.0, delivery_fee_verified=True)
              for r in "abcde"}

    result = optimize(items, offers, quotes)

    assert result.recommended.retailer_keys == ("a",)
    assert 0 < len(result.alternatives) <= 3


def test_empty_list_is_handled():
    result = optimize([], [], {})

    assert result.recommended is None
    assert "no items" in " ".join(result.notes)
