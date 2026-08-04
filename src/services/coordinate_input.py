"""Parsing and formatting for user-entered latitude/longitude pairs."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass


_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_DECIMAL_PAIR = re.compile(
    rf"^\s*(?P<latitude>{_NUMBER})\s+(?P<longitude>{_NUMBER})\s*$"
)
_HEMISPHERE = re.compile(r"[NSEW]", re.IGNORECASE)
_DMS_SYMBOLS = str.maketrans(
    {
        "°": " ",
        "º": " ",
        "'": " ",
        "’": " ",
        "′": " ",
        '"': " ",
        "”": " ",
        "″": " ",
    }
)


class CoordinateInputError(ValueError):
    """Raised when a user-entered coordinate pair cannot be interpreted."""


@dataclass(frozen=True, slots=True)
class CoordinatePair:
    """A latitude/longitude pair in decimal degrees."""

    latitude: float
    longitude: float


def _split_pair(value: str) -> tuple[str, str]:
    explicit_parts = re.split(r"\s*[,/]\s*", value.strip())
    if len(explicit_parts) == 2 and all(explicit_parts):
        return explicit_parts[0], explicit_parts[1]
    if len(explicit_parts) != 1:
        raise CoordinateInputError(
            "Enter exactly one latitude and one longitude."
        )

    decimal_match = _DECIMAL_PAIR.fullmatch(value)
    if decimal_match:
        return decimal_match.group("latitude"), decimal_match.group("longitude")

    directional_match = re.fullmatch(
        r"\s*(.+?[NS])\s*(.+?[EW])\s*",
        value,
        re.IGNORECASE,
    )
    if directional_match:
        return directional_match.group(1), directional_match.group(2)

    raise CoordinateInputError(
        "Separate latitude and longitude with a space, comma, or /."
    )


def _extract_hemisphere(value: str, axis: str) -> tuple[str, str | None]:
    matches = list(_HEMISPHERE.finditer(value))
    if not matches:
        return value, None
    if len(matches) != 1:
        raise CoordinateInputError(
            f"{axis.capitalize()} has multiple hemisphere markers."
        )

    match = matches[0]
    hemisphere = match.group(0).upper()
    allowed = {"N", "S"} if axis == "latitude" else {"E", "W"}
    if hemisphere not in allowed:
        expected = "N or S" if axis == "latitude" else "E or W"
        raise CoordinateInputError(
            f"{axis.capitalize()} must use {expected}, not {hemisphere}."
        )

    before = value[: match.start()].strip()
    after = value[match.end() :].strip()
    if before and after:
        raise CoordinateInputError(
            f"Put the {axis} hemisphere marker before or after its value."
        )
    return before or after, hemisphere


def _number(token: str, axis: str, part: str) -> float:
    if not re.fullmatch(_NUMBER, token):
        raise CoordinateInputError(
            f"{axis.capitalize()} {part} must be numeric."
        )
    result = float(token)
    if not math.isfinite(result):
        raise CoordinateInputError(f"{axis.capitalize()} must be finite.")
    return result


def _parse_component(value: str, axis: str) -> float:
    coordinate_text, hemisphere = _extract_hemisphere(value.strip(), axis)
    tokens = coordinate_text.translate(_DMS_SYMBOLS).split()
    if len(tokens) not in {1, 3}:
        raise CoordinateInputError(
            f"{axis.capitalize()} must be decimal degrees or DMS degrees, minutes, seconds."
        )

    degrees = _number(tokens[0], axis, "degrees")
    if len(tokens) == 1:
        magnitude = abs(degrees)
    else:
        minutes = _number(tokens[1], axis, "minutes")
        seconds = _number(tokens[2], axis, "seconds")
        if not degrees.is_integer():
            raise CoordinateInputError(
                f"{axis.capitalize()} DMS degrees must be a whole number."
            )
        if not minutes.is_integer():
            raise CoordinateInputError(
                f"{axis.capitalize()} DMS minutes must be a whole number."
            )
        if not 0 <= minutes < 60:
            raise CoordinateInputError(
                f"{axis.capitalize()} DMS minutes must be from 0 to less than 60."
            )
        if not 0 <= seconds < 60:
            raise CoordinateInputError(
                f"{axis.capitalize()} DMS seconds must be from 0 to less than 60."
            )
        magnitude = abs(degrees) + minutes / 60 + seconds / 3600

    if hemisphere is None:
        result = -magnitude if math.copysign(1.0, degrees) < 0 else magnitude
    else:
        hemisphere_sign = -1 if hemisphere in {"S", "W"} else 1
        if degrees < 0 and hemisphere_sign > 0:
            raise CoordinateInputError(
                f"{axis.capitalize()} sign conflicts with hemisphere {hemisphere}."
            )
        if tokens[0].startswith("+") and hemisphere_sign < 0:
            raise CoordinateInputError(
                f"{axis.capitalize()} sign conflicts with hemisphere {hemisphere}."
            )
        result = hemisphere_sign * magnitude

    limit = 90 if axis == "latitude" else 180
    if not -limit <= result <= limit:
        raise CoordinateInputError(
            f"{axis.capitalize()} must be from {-limit} to {limit} degrees."
        )
    return result


def parse_coordinate_pair(value: str) -> CoordinatePair:
    """Parse one latitude-first coordinate pair into decimal degrees."""
    if not isinstance(value, str) or not value.strip():
        raise CoordinateInputError("Enter a latitude and longitude.")
    latitude_text, longitude_text = _split_pair(value)
    return CoordinatePair(
        latitude=_parse_component(latitude_text, "latitude"),
        longitude=_parse_component(longitude_text, "longitude"),
    )


def format_coordinate_value(value: float) -> str:
    """Format decimal degrees with at most eight fractional digits."""
    numeric_value = float(value)
    if not math.isfinite(numeric_value):
        raise CoordinateInputError("Coordinates must be finite.")
    if numeric_value == 0:
        numeric_value = 0.0
    return f"{numeric_value:.8f}".rstrip("0").rstrip(".")


def format_coordinate_pair(pair: CoordinatePair) -> str:
    """Format a coordinate pair in the canonical latitude, longitude form."""
    return (
        f"{format_coordinate_value(pair.latitude)}, "
        f"{format_coordinate_value(pair.longitude)}"
    )
