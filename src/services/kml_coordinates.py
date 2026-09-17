"""Shared low-level inspection for KML coordinate values."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math


class CoordinateTokenIssue(str, Enum):
    ARITY = "arity"
    NON_NUMERIC = "non_numeric"
    NON_FINITE = "non_finite"
    LONGITUDE_OUT_OF_RANGE = "longitude_out_of_range"
    LATITUDE_OUT_OF_RANGE = "latitude_out_of_range"


@dataclass(frozen=True, slots=True)
class CoordinateTokenInspection:
    values: tuple[str, ...]
    numbers: tuple[float, ...] | None
    issue: CoordinateTokenIssue | None = None
    component: str | None = None
    problem_value: str | float | None = None

    @property
    def valid(self) -> bool:
        return self.issue is None and self.numbers is not None


def _inspect_values(
    values: tuple[str, ...],
    *,
    allowed_arities: tuple[int, ...],
) -> CoordinateTokenInspection:
    if len(values) not in allowed_arities or any(value == "" for value in values):
        return CoordinateTokenInspection(values, None, CoordinateTokenIssue.ARITY)

    component_names = ("longitude", "latitude", "altitude")
    numbers: list[float] = []
    for index, value in enumerate(values):
        component = component_names[index]
        try:
            number = float(value)
        except ValueError:
            return CoordinateTokenInspection(
                values,
                None,
                CoordinateTokenIssue.NON_NUMERIC,
                component,
                value,
            )
        if not math.isfinite(number):
            return CoordinateTokenInspection(
                values,
                None,
                CoordinateTokenIssue.NON_FINITE,
                component,
                value,
            )
        numbers.append(number)

    if not -180.0 <= numbers[0] <= 180.0:
        return CoordinateTokenInspection(
            values,
            tuple(numbers),
            CoordinateTokenIssue.LONGITUDE_OUT_OF_RANGE,
            "longitude",
            numbers[0],
        )
    if not -90.0 <= numbers[1] <= 90.0:
        return CoordinateTokenInspection(
            values,
            tuple(numbers),
            CoordinateTokenIssue.LATITUDE_OUT_OF_RANGE,
            "latitude",
            numbers[1],
        )
    return CoordinateTokenInspection(values, tuple(numbers))


def inspect_line_string_coordinate(token: str) -> CoordinateTokenInspection:
    """Inspect one comma-separated KML LineString coordinate token."""
    return _inspect_values(tuple(token.split(",")), allowed_arities=(2, 3))


def inspect_gx_coordinate(text: str) -> CoordinateTokenInspection:
    """Inspect one whitespace-separated gx:coord value."""
    return _inspect_values(tuple(text.split()), allowed_arities=(3,))


__all__ = [
    "CoordinateTokenInspection",
    "CoordinateTokenIssue",
    "inspect_gx_coordinate",
    "inspect_line_string_coordinate",
]
