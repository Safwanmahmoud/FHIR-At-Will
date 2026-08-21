"""Compact duration parsing for configuration values."""

from __future__ import annotations

from datetime import timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fhirbridge.util.duration import DurationParseError, format_duration, parse_duration


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0s", timedelta(0)),
        ("45s", timedelta(seconds=45)),
        ("90m", timedelta(minutes=90)),
        ("12h", timedelta(hours=12)),
        ("30d", timedelta(days=30)),
        ("2w", timedelta(weeks=2)),
        ("  30d  ", timedelta(days=30)),
    ],
)
def test_the_documented_forms_parse(text: str, expected: timedelta) -> None:
    assert parse_duration(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "30",
        "d",
        "-30d",
        "30 d",
        "30D",
        "1.5h",
        "30days",
        "1h30m",
        "P30D",
        "thirty days",
    ],
)
def test_anything_else_is_rejected(text: str) -> None:
    """A silently misparsed retention period is a data-retention incident, so the
    grammar is deliberately narrow and every near-miss is an error."""
    with pytest.raises(DurationParseError) as caught:
        parse_duration(text)

    assert "expected an integer followed by one of s, m, h, d, w" in str(caught.value)


def test_a_negative_duration_is_not_expressible() -> None:
    with pytest.raises(DurationParseError):
        parse_duration("-1d")


def test_formatting_picks_the_largest_exact_unit() -> None:
    assert format_duration(timedelta(weeks=2)) == "2w"
    assert format_duration(timedelta(days=30)) == "30d"
    assert format_duration(timedelta(hours=12)) == "12h"
    assert format_duration(timedelta(minutes=90)) == "90m"
    assert format_duration(timedelta(seconds=45)) == "45s"
    assert format_duration(timedelta(0)) == "0s"


def test_a_duration_with_no_exact_unit_falls_back_to_seconds() -> None:
    assert format_duration(timedelta(hours=1, seconds=30)) == "3630s"


@given(st.integers(min_value=0, max_value=10_000), st.sampled_from("smhdw"))
def test_parsing_and_formatting_round_trip_to_the_same_duration(value: int, unit: str) -> None:
    """Formatting may choose a different unit (60m is 1h), but the duration it
    denotes must be identical — that is what a round trip has to preserve."""
    original = parse_duration(f"{value}{unit}")

    assert parse_duration(format_duration(original)) == original
