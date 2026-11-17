"""Public ids (ADR 0033): their shape, their randomness and what is not an id."""

import re

import pytest

from core import public_ids


def test_an_id_is_the_prefix_and_the_hex_of_a_random_uuid4() -> None:
    value = public_ids.generate("orig")

    assert re.fullmatch(r"orig_[0-9a-f]{32}", value)
    assert value[5 + 12] == "4", "a UUIDv4 carries its version nibble"
    assert public_ids.is_valid("orig", value)


def test_ids_do_not_repeat() -> None:
    values = {public_ids.generate("page") for _ in range(10_000)}

    assert len(values) == 10_000


@pytest.mark.parametrize(
    "value",
    [
        7,
        "7",
        "",
        "orig_",
        "org_" + "a" * 32,  # another kind
        "orig_" + "A" * 32,  # upper case is another string
        "orig_" + "a" * 31,
        "orig_" + "a" * 33,
        "orig_" + "g" * 32,
        " orig_" + "a" * 32,
        "orig_" + "a" * 32 + "\n",
        "../orig_" + "a" * 32,
    ],
)
def test_anything_else_is_not_an_id_of_that_kind(value: object) -> None:
    assert not public_ids.is_valid("orig", value)


def test_the_missing_id_is_well_formed_and_never_generated() -> None:
    missing = public_ids.missing("orig")

    assert public_ids.is_valid("orig", missing)
    assert missing[5 + 12] != "4"


@pytest.mark.parametrize("prefix", ["", "o", "Orig", "orig-x", "toolongprefix", "or1g"])
def test_a_prefix_is_two_to_eight_lowercase_letters(prefix: str) -> None:
    with pytest.raises(ValueError):
        public_ids.generate(prefix)
    with pytest.raises(ValueError):
        public_ids.public_id_field(prefix)
