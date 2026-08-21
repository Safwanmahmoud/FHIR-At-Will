"""Parsing for the compact duration strings used in configuration (``30d``, ``12h``)."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Final

_PATTERN: Final = re.compile(r"^(?P<value>\d+)(?P<unit>[smhdw])$")

_UNITS: Final[dict[str, str]] = {
    "s": "seconds",
    "m": "minutes",
    "h": "hours",
    "d": "days",
    "w": "weeks",
}


class DurationParseError(ValueError):
    """Raised when a duration string does not match ``<integer><s|m|h|d|w>``."""


def parse_duration(value: str) -> timedelta:
    """Parse ``30d`` / ``90m`` / ``0s`` into a :class:`~datetime.timedelta`.

    Zero is permitted and means "do not retain"; negative values are not
    expressible by the grammar, which is intentional.
    """
    match = _PATTERN.match(value.strip())
    if match is None:
        raise DurationParseError(
            f"invalid duration {value!r}: expected an integer followed by one of s, m, h, d, w"
        )
    return timedelta(**{_UNITS[match["unit"]]: int(match["value"])})


def format_duration(value: timedelta) -> str:
    """Render a timedelta back into the largest exact compact unit."""
    total = int(value.total_seconds())
    for unit, seconds in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60)):
        if total and total % seconds == 0:
            return f"{total // seconds}{unit}"
    return f"{total}s"


__all__ = ["DurationParseError", "format_duration", "parse_duration"]
