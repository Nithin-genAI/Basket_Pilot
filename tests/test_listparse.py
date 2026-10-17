"""Unit tests for natural-language shopping list parsing.

The bar these hold the parser to: it may leave something unknown, but it must never
invent a quantity or a brand that the user did not write. A fabricated quantity flows
straight into the optimizer as a constraint nobody set.
"""

from __future__ import annotations

import pytest

from basketpilot.listparse import parse_item, parse_list, to_queries


def test_quantity_and_unit_are_parsed():
    item = parse_item("2 litres of Amul milk")

    assert item.quantity == 2000.0
    assert item.unit == "ml"
    assert "milk" in item.name.lower()


def test_shorthand_unit_is_parsed():
    item = parse_item("2L milk")

    assert item.quantity == 2000.0
    assert item.unit == "ml"


def test_word_quantity_dozen():
    item = parse_item("a dozen eggs")

    assert item.quantity == 12.0
    assert item.unit == "count"
    assert item.name.lower() == "eggs"


def test_word_quantity_number():
    item = parse_item("two onions")

    assert item.quantity == 2.0
    assert item.name.lower() == "onions"


def test_half_kilo():
    item = parse_item("half kg paneer")

    assert item.quantity == 500.0
    assert item.unit == "g"


def test_brand_is_extracted_from_capitalisation():
    item = parse_item("2L Amul milk")

    assert item.brand == "Amul"
    assert item.name.lower() == "milk"
    # Capitalisation alone is weak evidence, so it must not become a hard constraint.
    assert item.brand_required is False


def test_the_search_query_carries_the_brand():
    item = parse_item("2L Amul milk")

    assert item.search_query == "Amul milk"


def test_only_marker_makes_a_brand_required():
    item = parse_item("only Amul milk")

    assert item.brand == "Amul"
    assert item.brand_required is True


def test_no_quantity_is_left_unknown_not_invented():
    """The rule that matters most: 'milk' is not '1 milk'."""
    item = parse_item("milk")

    assert item.quantity is None
    assert item.name.lower() == "milk"


def test_filler_words_are_dropped():
    item = parse_item("please buy some fresh milk")

    assert "please" not in item.name.lower()
    assert "milk" in item.name.lower()


def test_trailing_size_is_a_pack_preference_not_a_count():
    """'milk 1L' means one pack of a litre, so it must not become a quantity of 1."""
    item = parse_item("milk 1L")

    assert item.quantity is None
    assert item.unit == "ml"
    assert "milk" in item.name.lower()


def test_full_list_splits_on_commas():
    items = parse_list("2 litres of Amul milk, a dozen eggs, brown bread")

    assert len(items) == 3
    assert items[0].quantity == 2000.0
    assert items[1].quantity == 12.0
    assert "bread" in items[2].name.lower()


def test_list_splits_on_newlines_and_semicolons():
    items = parse_list("milk\nbread; eggs")

    assert len(items) == 3


def test_duplicates_collapse():
    items = parse_list("milk, milk, Milk")

    assert len(items) == 1


def test_item_ids_are_unique():
    items = parse_list("milk, bread, eggs, milk")

    assert len({item.item_id for item in items}) == len(items)


def test_queries_are_deduplicated():
    items = parse_list("milk, milk, bread")

    assert to_queries(items) == ["milk", "bread"]


def test_empty_input_yields_no_items():
    assert parse_list("") == []
    assert parse_list("  ,  , ") == []


def test_raw_text_is_preserved_for_audit():
    item = parse_item("2 litres of Amul milk")

    assert item.raw == "2 litres of Amul milk"
