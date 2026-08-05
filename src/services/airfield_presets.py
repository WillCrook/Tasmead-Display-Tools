"""Typed airfield preset payloads and directional runway validation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
import re

from .coordinate_input import CoordinatePair
from .runway_alignment import RunwayReference


_CONVENTIONAL_RUNWAY = re.compile(
    r"^(?P<number>0[1-9]|[12][0-9]|3[0-6])(?P<suffix>[LCR])?$",
    re.IGNORECASE,
)


class AirfieldPresetError(ValueError):
    """Raised when an airfield payload cannot be used or saved safely."""


@dataclass(frozen=True, slots=True)
class RunwayDesignator:
    """A trimmed runway identifier and whether it follows the standard form."""

    value: str
    conventional: bool


def normalise_runway_designator(value: object) -> RunwayDesignator:
    """Trim a designator and uppercase only a conventional L/C/R suffix."""
    text = "" if value is None else str(value).strip()
    match = _CONVENTIONAL_RUNWAY.fullmatch(text)
    if match is None:
        return RunwayDesignator(text, False)
    suffix = (match.group("suffix") or "").upper()
    return RunwayDesignator(f"{match.group('number')}{suffix}", True)


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _optional_float(value: object, label: str) -> float | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise AirfieldPresetError(f"{label} must be numeric.") from error
    if not math.isfinite(result):
        raise AirfieldPresetError(f"{label} must be finite.")
    return result


@dataclass(frozen=True, slots=True)
class AirfieldPresetData:
    """One directional airfield runway stored in a preset envelope."""

    airfield_name: str
    runway: str
    threshold_latitude: float | None
    threshold_longitude: float | None
    true_heading_deg: float | None
    elevation_m: float | None

    @classmethod
    def from_mapping(
        cls, data: Mapping[str, object]
    ) -> tuple["AirfieldPresetData", tuple[str, ...]]:
        """Decode canonical or legacy data without mutating the source file."""
        canonical = any(
            key in data
            for key in (
                "airfield_name",
                "threshold_latitude",
                "threshold_longitude",
                "true_heading_deg",
                "elevation_m",
            )
        )
        warnings: list[str] = []
        if canonical:
            airfield_name = _text(data.get("airfield_name"))
            runway = normalise_runway_designator(data.get("runway")).value
            latitude = _optional_float(
                data.get("threshold_latitude"), "Departure threshold latitude"
            )
            longitude = _optional_float(
                data.get("threshold_longitude"), "Departure threshold longitude"
            )
            heading = _optional_float(data.get("true_heading_deg"), "True heading")
            elevation = _optional_float(data.get("elevation_m"), "Elevation")
        else:
            airfield_name = _text(data.get("name"))
            runway = normalise_runway_designator(data.get("runway")).value
            latitude = _optional_float(data.get("latitude"), "Departure threshold latitude")
            longitude = _optional_float(
                data.get("longitude"), "Departure threshold longitude"
            )
            heading = _optional_float(data.get("heading"), "True heading")
            elevation = None
            if _text(data.get("original_elevation_m")):
                warnings.append(
                    "The legacy source fallback elevation was not treated as this "
                    "airfield's elevation. Enter and save the surveyed airfield elevation."
                )
            warnings.append(
                "This preset uses the legacy airfield payload and will be converted only "
                "when you explicitly save it."
            )
        if latitude is not None and longitude is not None and heading is not None:
            try:
                reference = RunwayReference(latitude, longitude, heading, elevation)
            except ValueError as error:
                raise AirfieldPresetError(str(error)) from error
            latitude = reference.latitude
            longitude = reference.longitude
            heading = reference.true_heading_deg
            elevation = reference.elevation_m
        return (
            cls(
                airfield_name=airfield_name,
                runway=runway,
                threshold_latitude=latitude,
                threshold_longitude=longitude,
                true_heading_deg=heading,
                elevation_m=elevation,
            ),
            tuple(warnings),
        )

    @classmethod
    def validated(
        cls,
        *,
        airfield_name: object,
        runway: object,
        threshold: CoordinatePair,
        true_heading_deg: object,
        elevation_m: object,
    ) -> "AirfieldPresetData":
        """Build a complete payload suitable for creating or updating a preset."""
        name = _text(airfield_name)
        if not name:
            raise AirfieldPresetError("Enter an airfield name.")
        designator = normalise_runway_designator(runway)
        if not designator.value:
            raise AirfieldPresetError("Enter an airfield runway identifier.")
        heading = _optional_float(true_heading_deg, "True heading")
        if heading is None:
            raise AirfieldPresetError("Enter a true heading.")
        elevation = _optional_float(elevation_m, "Elevation")
        if elevation is None:
            raise AirfieldPresetError("Enter the airfield elevation.")
        try:
            reference = RunwayReference(
                threshold.latitude,
                threshold.longitude,
                heading,
                elevation,
            )
        except ValueError as error:
            raise AirfieldPresetError(str(error)) from error
        return cls(
            airfield_name=name,
            runway=designator.value,
            threshold_latitude=reference.latitude,
            threshold_longitude=reference.longitude,
            true_heading_deg=reference.true_heading_deg,
            elevation_m=reference.elevation_m,
        )

    def to_mapping(self) -> dict[str, object]:
        """Return the canonical version-1 airfield payload."""
        return {
            "airfield_name": self.airfield_name,
            "runway": self.runway,
            "threshold_latitude": self.threshold_latitude,
            "threshold_longitude": self.threshold_longitude,
            "true_heading_deg": self.true_heading_deg,
            "elevation_m": self.elevation_m,
        }


__all__ = [
    "AirfieldPresetData",
    "AirfieldPresetError",
    "RunwayDesignator",
    "normalise_runway_designator",
]
