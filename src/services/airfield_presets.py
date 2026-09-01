"""Typed airfield preset payloads and directional runway validation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
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


TRANSPOSITION_PRESET_DATA_VERSION = 2
_TRANSPOSITION_FIELDS = {
    "data_version",
    "runway",
    "original_trace",
    "target_trace",
}


@dataclass(frozen=True, slots=True)
class RunwayPresetSection:
    """Reusable runway geometry for either runway-alignment card."""

    threshold_latitude: float
    threshold_longitude: float
    true_heading_deg: float
    elevation_m: float | None = None

    @classmethod
    def validated(
        cls,
        *,
        threshold: CoordinatePair,
        true_heading_deg: object,
        elevation_m: object = None,
        elevation_required: bool = False,
    ) -> "RunwayPresetSection":
        heading = _optional_float(true_heading_deg, "True heading")
        if heading is None:
            raise AirfieldPresetError("Enter a true heading.")
        elevation = _optional_float(elevation_m, "Elevation")
        if elevation_required and elevation is None:
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
            reference.latitude,
            reference.longitude,
            reference.true_heading_deg,
            reference.elevation_m,
        )

    @classmethod
    def from_mapping(cls, data: object) -> "RunwayPresetSection":
        if not isinstance(data, Mapping):
            raise AirfieldPresetError("Runway preset data must be an object.")
        expected = {
            "threshold_latitude",
            "threshold_longitude",
            "true_heading_deg",
            "elevation_m",
        }
        if set(data) != expected:
            raise AirfieldPresetError(
                "Runway preset data contains unsupported or missing fields."
            )
        latitude = _optional_float(data["threshold_latitude"], "Departure threshold latitude")
        longitude = _optional_float(data["threshold_longitude"], "Departure threshold longitude")
        heading = _optional_float(data["true_heading_deg"], "True heading")
        if latitude is None or longitude is None or heading is None:
            raise AirfieldPresetError(
                "Runway presets require a departure threshold and true heading."
            )
        elevation = _optional_float(data["elevation_m"], "Elevation")
        try:
            reference = RunwayReference(latitude, longitude, heading, elevation)
        except ValueError as error:
            raise AirfieldPresetError(str(error)) from error
        return cls(
            reference.latitude,
            reference.longitude,
            reference.true_heading_deg,
            reference.elevation_m,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "threshold_latitude": self.threshold_latitude,
            "threshold_longitude": self.threshold_longitude,
            "true_heading_deg": self.true_heading_deg,
            "elevation_m": self.elevation_m,
        }


@dataclass(frozen=True, slots=True)
class OriginalTracePresetSection:
    """The only user-editable source-trace value."""

    ground_elevation_m: float

    @classmethod
    def validated(cls, value: object) -> "OriginalTracePresetSection":
        elevation = _optional_float(value, "Ground reference elevation")
        if elevation is None:
            raise AirfieldPresetError("Enter the ground reference elevation.")
        return cls(elevation)

    @classmethod
    def from_mapping(cls, data: object) -> "OriginalTracePresetSection":
        if not isinstance(data, Mapping) or set(data) != {"ground_elevation_m"}:
            raise AirfieldPresetError(
                "Original trace preset data contains unsupported or missing fields."
            )
        return cls.validated(data["ground_elevation_m"])

    def to_mapping(self) -> dict[str, object]:
        return {"ground_elevation_m": self.ground_elevation_m}


@dataclass(frozen=True, slots=True)
class TargetTracePresetSection:
    """Manual destination anchor and clockwise rotation."""

    target_latitude: float
    target_longitude: float
    rotation_deg: float

    @classmethod
    def validated(
        cls,
        *,
        target: CoordinatePair,
        rotation_deg: object,
    ) -> "TargetTracePresetSection":
        rotation = _optional_float(rotation_deg, "Clockwise rotation")
        if rotation is None or not 0.0 <= rotation <= 360.0:
            raise AirfieldPresetError(
                "Clockwise rotation must be between 0 and 360 degrees."
            )
        try:
            reference = RunwayReference(
                target.latitude,
                target.longitude,
                0.0,
            )
        except ValueError as error:
            raise AirfieldPresetError(str(error)) from error
        return cls(reference.latitude, reference.longitude, rotation)

    @classmethod
    def from_mapping(cls, data: object) -> "TargetTracePresetSection":
        if not isinstance(data, Mapping) or set(data) != {
            "target_latitude",
            "target_longitude",
            "rotation_deg",
        }:
            raise AirfieldPresetError(
                "Target trace preset data contains unsupported or missing fields."
            )
        latitude = _optional_float(data["target_latitude"], "Target latitude")
        longitude = _optional_float(data["target_longitude"], "Target longitude")
        if latitude is None or longitude is None:
            raise AirfieldPresetError("Target trace presets require coordinates.")
        try:
            target = CoordinatePair(latitude, longitude)
        except ValueError as error:
            raise AirfieldPresetError(str(error)) from error
        return cls.validated(target=target, rotation_deg=data["rotation_deg"])

    def to_mapping(self) -> dict[str, object]:
        return {
            "target_latitude": self.target_latitude,
            "target_longitude": self.target_longitude,
            "rotation_deg": self.rotation_deg,
        }


@dataclass(frozen=True, slots=True)
class TranspositionPresetData:
    """Shared, partial data that can be built up from any alignment card."""

    runway: RunwayPresetSection | None = None
    original_trace: OriginalTracePresetSection | None = None
    target_trace: TargetTracePresetSection | None = None

    @classmethod
    def from_mapping(
        cls, data: Mapping[str, object]
    ) -> tuple["TranspositionPresetData", tuple[str, ...]]:
        if set(data) == _TRANSPOSITION_FIELDS:
            if data["data_version"] != TRANSPOSITION_PRESET_DATA_VERSION:
                raise AirfieldPresetError(
                    f'Unsupported transposition preset data_version {data["data_version"]!r}.'
                )
            return (
                cls(
                    runway=(
                        None
                        if data["runway"] is None
                        else RunwayPresetSection.from_mapping(data["runway"])
                    ),
                    original_trace=(
                        None
                        if data["original_trace"] is None
                        else OriginalTracePresetSection.from_mapping(
                            data["original_trace"]
                        )
                    ),
                    target_trace=(
                        None
                        if data["target_trace"] is None
                        else TargetTracePresetSection.from_mapping(data["target_trace"])
                    ),
                ),
                (),
            )

        if set(data) & {"data_version", "original_trace", "target_trace"}:
            raise AirfieldPresetError(
                "Transposition preset data contains unsupported or missing fields."
            )

        legacy, warnings = AirfieldPresetData.from_mapping(data)
        if (
            legacy.threshold_latitude is None
            or legacy.threshold_longitude is None
            or legacy.true_heading_deg is None
        ):
            raise AirfieldPresetError(
                "This legacy preset does not contain complete runway geometry."
            )
        runway = RunwayPresetSection.from_mapping(
            {
                "threshold_latitude": legacy.threshold_latitude,
                "threshold_longitude": legacy.threshold_longitude,
                "true_heading_deg": legacy.true_heading_deg,
                "elevation_m": legacy.elevation_m,
            }
        )
        return cls(runway=runway), warnings

    def with_runway(self, runway: RunwayPresetSection) -> "TranspositionPresetData":
        return replace(self, runway=runway)

    def with_original_trace(
        self, original_trace: OriginalTracePresetSection
    ) -> "TranspositionPresetData":
        return replace(self, original_trace=original_trace)

    def with_target_trace(
        self, target_trace: TargetTracePresetSection
    ) -> "TranspositionPresetData":
        return replace(self, target_trace=target_trace)

    def to_mapping(self) -> dict[str, object]:
        return {
            "data_version": TRANSPOSITION_PRESET_DATA_VERSION,
            "runway": None if self.runway is None else self.runway.to_mapping(),
            "original_trace": (
                None
                if self.original_trace is None
                else self.original_trace.to_mapping()
            ),
            "target_trace": (
                None if self.target_trace is None else self.target_trace.to_mapping()
            ),
        }


__all__ = [
    "AirfieldPresetData",
    "AirfieldPresetError",
    "OriginalTracePresetSection",
    "RunwayPresetSection",
    "RunwayDesignator",
    "TRANSPOSITION_PRESET_DATA_VERSION",
    "TargetTracePresetSection",
    "TranspositionPresetData",
    "normalise_runway_designator",
]
