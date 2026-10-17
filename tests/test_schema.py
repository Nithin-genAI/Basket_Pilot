"""Unit tests for the Phase 2 offer schema: pack sizes, unit prices, fee honesty.

The recurring theme is that a missing value stays missing. Almost every test here is
a variation on "this must not become zero".
"""

from __future__ import annotations

import pytest

from basketpilot.offers import (
    RetailerQuote,
    parse_pack_size,
    unit_label,
    unit_price,
)

from conftest import make_item, make_offer, make_quote


# --------------------------------------------------------------------------- #
# Pack sizes
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text,expected",
    [
        ("500 ml", (500.0, "ml")),
        ("1 L", (1000.0, "ml")),
        ("1ltr", (1000.0, "ml")),
        ("1.5 L", (1500.0, "ml")),
        ("500 g", (500.0, "g")),
        ("1 kg", (1000.0, "g")),
        ("250 g", (250.0, "g")),
        ("12 pcs", (12.0, "count")),
        ("1 dozen", (12.0, "count")),
    ],
)
def test_pack_sizes_normalise(text, expected):
    assert parse_pack_size(text) == expected


def test_multipack_multiplies():
    assert parse_pack_size("6 x 100 g") == (600.0, "g")


def test_pack_size_embedded_in_a_name_is_found():
    assert parse_pack_size("Amul Taaza Toned Milk 1 L") == (1000.0, "ml")


@pytest.mark.parametrize("text", [None, "", "MRP", "combo pack", "no size here"])
def test_unreadable_pack_size_stays_unknown(text):
    assert parse_pack_size(text) == (None, None)


# --------------------------------------------------------------------------- #
# Unit prices
# --------------------------------------------------------------------------- #


def test_unit_price_per_100g():
    assert unit_price(50.0, 500.0, "g") == 10.0


def test_unit_price_per_litre():
    assert unit_price(60.0, 500.0, "ml") == 120.0


def test_unit_price_per_item():
    assert unit_price(84.0, 12.0, "count") == 7.0


def test_unit_price_is_unknown_when_anything_is_missing():
    assert unit_price(None, 500.0, "g") is None
    assert unit_price(50.0, None, "g") is None
    assert unit_price(50.0, 500.0, None) is None


def test_unit_price_does_not_divide_by_zero():
    assert unit_price(50.0, 0.0, "g") is None


def test_unit_labels_are_human_readable():
    assert unit_label("g") == "per 100 g"
    assert unit_label("ml") == "per litre"
    assert unit_label("count") == "per item"
    assert unit_label(None) == ""


# --------------------------------------------------------------------------- #
# Offers
# --------------------------------------------------------------------------- #


def test_an_offer_derives_its_pack_size_and_unit_price():
    item = make_item("milk")
    offer = make_offer("blinkit", item, name="Amul Taaza Milk", price=60.0,
                       pack_size="500 ml", brand="Amul")

    assert offer.pack_quantity == 500.0
    assert offer.pack_unit == "ml"
    assert offer.unit_price == 120.0
    assert offer.price == 60.0


def test_an_offer_with_an_unreadable_pack_has_no_unit_price():
    item = make_item("milk")
    offer = make_offer("blinkit", item, name="Mystery Milk", price=60.0, pack_size=None)

    assert offer.pack_quantity is None
    assert offer.unit_price is None


def test_offer_serialisation_carries_the_evidence():
    item = make_item("milk")
    offer = make_offer("blinkit", item, name="Amul Taaza Milk", price=60.0,
                       pack_size="500 ml", brand="Amul")
    payload = offer.to_dict()

    assert payload["price"] == 60.0
    assert payload["price_displayed"] == "₹60"
    assert payload["unit_price"] == 120.0
    assert payload["unit_price_label"] == "per litre"
    assert payload["engine"] == "test_fixture"
    assert payload["source"] == "synthetic"


# --------------------------------------------------------------------------- #
# Quotes — the honesty rules
# --------------------------------------------------------------------------- #


def test_a_fresh_quote_verifies_nothing():
    quote = make_quote("blinkit")

    assert quote.delivery_fee is None
    assert quote.delivery_fee_verified is False
    unknown = quote.unverified_charges()
    assert any("delivery fee" in u for u in unknown)
    assert any("handling fee" in u for u in unknown)
    assert any("discount" in u for u in unknown)


def test_an_unverified_fee_contributes_nothing():
    quote = make_quote("blinkit", delivery_fee=40.0, delivery_fee_verified=False)

    assert quote.verified_fees(100.0) == 0.0


def test_a_verified_fee_contributes():
    quote = make_quote("blinkit", delivery_fee=40.0, delivery_fee_verified=True)

    assert quote.verified_fees(100.0) == 40.0


def test_free_delivery_threshold_waives_the_fee():
    quote = make_quote("blinkit", delivery_fee=40.0, delivery_fee_verified=True,
                       free_delivery_threshold=199.0)

    assert quote.verified_fees(250.0) == 0.0
    assert quote.verified_fees(100.0) == 40.0


def test_small_cart_fee_applies_below_the_minimum():
    quote = make_quote("blinkit", small_cart_fee=20.0, small_cart_fee_verified=True,
                       min_order=100.0)

    assert quote.verified_fees(50.0) == 20.0
    assert quote.verified_fees(150.0) == 0.0


def test_unverified_discount_is_not_applied():
    quote = make_quote("blinkit", discount=50.0, discount_verified=False)

    assert quote.verified_discount() == 0.0


def test_verified_discount_is_applied():
    quote = make_quote("blinkit", discount=50.0, discount_verified=True)

    assert quote.verified_discount() == 50.0


def test_quote_serialisation_names_every_unknown():
    quote = make_quote("blinkit", delivery_fee=40.0, delivery_fee_verified=True)
    payload = quote.to_dict()

    assert payload["delivery_fee_verified"] is True
    # Delivery is verified, so it must not be listed as unknown; the rest must be.
    assert not any("delivery fee" in u for u in payload["unverified_charges"])
    assert any("handling fee" in u for u in payload["unverified_charges"])


# --------------------------------------------------------------------------- #
# Items
# --------------------------------------------------------------------------- #


def test_item_serialisation_round_trips_the_constraints():
    item = make_item("milk", brand="Amul", brand_required=True)
    payload = item.to_dict()

    assert payload["brand"] == "Amul"
    assert payload["brand_required"] is True
