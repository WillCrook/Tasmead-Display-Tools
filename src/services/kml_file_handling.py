"""Strict, shared parsing for KML flight paths."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
import xml.etree.ElementTree as ET


KML_NAMESPACE = "http://www.opengis.net/kml/2.2"
GX_NAMESPACE = "http://www.google.com/kml/ext/2.2"


class KmlParseError(ValueError):
    """Base class for user-correctable KML parsing failures."""


class KmlXmlError(KmlParseError):
    """Raised when the input is not well-formed XML."""


class KmlStructureError(KmlParseError):
    """Raised when the XML does not contain one supported flight path."""


class KmlCoordinateError(KmlParseError):
    """Raised when a supported path contains an invalid coordinate."""


@dataclass(frozen=True, slots=True)
class KmlPoint:
    """A KML position normalized to latitude/longitude ordering."""

    latitude: float
    longitude: float
    altitude_m: float | None
    timestamp: str | None = None


@dataclass(frozen=True, slots=True)
class KmlTrack:
    """The single flight-path geometry selected from a KML document."""

    points: tuple[KmlPoint, ...]
    geometry_kind: Literal["line_string", "gx_track"]
    placemark_name: str | None
    altitude_mode: str = "clampToGround"


@dataclass(frozen=True, slots=True)
class _TrackCandidate:
    element: ET.Element
    geometry_kind: Literal["line_string", "gx_track"]
    placemark_name: str | None


def _qualified(namespace: str, local_name: str) -> str:
    return f"{{{namespace}}}{local_name}" if namespace else local_name


def _split_tag(tag: str) -> tuple[str, str]:
    if tag.startswith("{"):
        namespace, local_name = tag[1:].split("}", 1)
        return namespace, local_name
    return "", tag


def _placemark_name(placemark: ET.Element, namespace: str) -> str | None:
    name_tag = _qualified(namespace, "name")
    for child in placemark:
        if child.tag == name_tag and child.text:
            name = child.text.strip()
            return name or None
    return None


def _context(
    path: Path,
    candidate: _TrackCandidate,
    tuple_index: int | None = None,
) -> str:
    name = candidate.placemark_name or "unnamed Placemark"
    geometry = "LineString" if candidate.geometry_kind == "line_string" else "gx:Track"
    context = f"{path.name}: {name} ({geometry})"
    if tuple_index is not None:
        context += f", coordinate {tuple_index}"
    return context


def _parse_number(
    value: str,
    component: str,
    path: Path,
    candidate: _TrackCandidate,
    tuple_index: int,
) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise KmlCoordinateError(
            f'{_context(path, candidate, tuple_index)}: {component} value "{value}" is not numeric.'
        ) from exc

    if not math.isfinite(number):
        raise KmlCoordinateError(
            f'{_context(path, candidate, tuple_index)}: {component} value "{value}" is not finite.'
        )
    return number


def _make_point(
    values: list[str],
    path: Path,
    candidate: _TrackCandidate,
    tuple_index: int,
) -> KmlPoint:
    longitude = _parse_number(values[0], "longitude", path, candidate, tuple_index)
    latitude = _parse_number(values[1], "latitude", path, candidate, tuple_index)
    altitude = (
        _parse_number(values[2], "altitude", path, candidate, tuple_index)
        if len(values) == 3
        else None
    )

    if not -180.0 <= longitude <= 180.0:
        raise KmlCoordinateError(
            f"{_context(path, candidate, tuple_index)}: longitude {longitude} is outside -180 to 180."
        )
    if not -90.0 <= latitude <= 90.0:
        raise KmlCoordinateError(
            f"{_context(path, candidate, tuple_index)}: latitude {latitude} is outside -90 to 90."
        )
    return KmlPoint(latitude=latitude, longitude=longitude, altitude_m=altitude)


def _parse_line_string(
    path: Path,
    candidate: _TrackCandidate,
    namespace: str,
) -> tuple[KmlPoint, ...]:
    coordinates_tag = _qualified(namespace, "coordinates")
    containers = [child for child in candidate.element if child.tag == coordinates_tag]
    if len(containers) != 1:
        raise KmlStructureError(
            f"{_context(path, candidate)}: expected exactly one coordinates element, found {len(containers)}."
        )

    text = containers[0].text
    tokens = text.split() if text else []
    if not tokens:
        raise KmlCoordinateError(f"{_context(path, candidate)}: coordinates element is empty.")

    points: list[KmlPoint] = []
    for index, token in enumerate(tokens, start=1):
        values = token.split(",")
        if len(values) not in (2, 3) or any(value == "" for value in values):
            raise KmlCoordinateError(
                f'{_context(path, candidate, index)}: "{token}" must contain longitude,latitude '
                "and optional altitude."
            )
        points.append(_make_point(values, path, candidate, index))
    return tuple(points)


def _altitude_mode(
    path: Path,
    candidate: _TrackCandidate,
    namespace: str,
) -> str:
    supported = {
        "absolute",
        "relativeToGround",
        "clampToGround",
        "relativeToSeaFloor",
        "clampToSeaFloor",
    }
    tags = {
        _qualified(namespace, "altitudeMode"),
        _qualified(GX_NAMESPACE, "altitudeMode"),
    }
    for child in candidate.element:
        if child.tag in tags and child.text:
            value = child.text.strip()
            if value in supported:
                return value
            raise KmlStructureError(
                f'{_context(path, candidate)}: unsupported altitude mode "{value}".'
            )
    return "clampToGround"


def _parse_gx_track(path: Path, candidate: _TrackCandidate) -> tuple[KmlPoint, ...]:
    coord_tag = _qualified(GX_NAMESPACE, "coord")
    elements = [child for child in candidate.element if child.tag == coord_tag]
    if not elements:
        raise KmlCoordinateError(f"{_context(path, candidate)}: gx:Track contains no gx:coord elements.")

    when_tags = {_qualified(KML_NAMESPACE, "when"), "when"}
    timestamps = [
        child.text.strip() if child.text and child.text.strip() else None
        for child in candidate.element
        if child.tag in when_tags
    ]
    if len(timestamps) != len(elements):
        timestamps = [None] * len(elements)

    points: list[KmlPoint] = []
    for index, element in enumerate(elements, start=1):
        text = element.text.strip() if element.text else ""
        if not text:
            raise KmlCoordinateError(
                f"{_context(path, candidate, index)}: empty gx:coord values require "
                "interpolation, which is not supported."
            )
        values = text.split()
        if len(values) != 3:
            raise KmlCoordinateError(
                f'{_context(path, candidate, index)}: "{text}" must contain longitude latitude altitude.'
            )
        point = _make_point(values, path, candidate, index)
        points.append(
            KmlPoint(
                latitude=point.latitude,
                longitude=point.longitude,
                altitude_m=point.altitude_m,
                timestamp=timestamps[index - 1],
            )
        )
    return tuple(points)


def parse_kml_track(file_path: str | os.PathLike[str]) -> KmlTrack:
    """Parse exactly one KML LineString or gx:Track into a shared track model.

    Altitudes are returned exactly as encoded. A two-dimensional LineString
    coordinate has ``altitude_m=None``; no altitude-mode or terrain conversion
    is performed.
    """
    path = Path(file_path)
    if path.suffix.lower() == ".kmz":
        raise KmlStructureError(f"{path.name}: KMZ archives are not supported; select a KML file.")

    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        line, column = getattr(exc, "position", (None, None))
        location = f" at line {line}, column {column}" if line is not None else ""
        raise KmlXmlError(f"{path.name}: invalid XML{location}: {exc}.") from exc

    namespace, local_name = _split_tag(root.tag)
    if local_name != "kml":
        raise KmlStructureError(f'{path.name}: expected a kml root element, found "{local_name}".')
    if namespace not in ("", KML_NAMESPACE):
        raise KmlStructureError(f'{path.name}: unsupported KML namespace "{namespace}".')

    placemark_tag = _qualified(namespace, "Placemark")
    line_string_tag = _qualified(namespace, "LineString")
    gx_track_tag = _qualified(GX_NAMESPACE, "Track")
    candidates: list[_TrackCandidate] = []

    for placemark in root.iter(placemark_tag):
        name = _placemark_name(placemark, namespace)
        for element in placemark.iter():
            if element.tag == line_string_tag:
                candidates.append(_TrackCandidate(element, "line_string", name))
            elif element.tag == gx_track_tag:
                candidates.append(_TrackCandidate(element, "gx_track", name))

    if not candidates:
        raise KmlStructureError(
            f"{path.name}: no supported LineString or gx:Track flight path was found inside a Placemark."
        )
    if len(candidates) > 1:
        descriptions = [
            f'{candidate.placemark_name or "unnamed Placemark"} '
            f'({"LineString" if candidate.geometry_kind == "line_string" else "gx:Track"})'
            for candidate in candidates
        ]
        raise KmlStructureError(
            f"{path.name}: found {len(candidates)} flight paths; exactly one is required: "
            + "; ".join(descriptions)
            + "."
        )

    candidate = candidates[0]
    if candidate.geometry_kind == "line_string":
        points = _parse_line_string(path, candidate, namespace)
    else:
        points = _parse_gx_track(path, candidate)

    if len(points) < 2:
        raise KmlStructureError(
            f"{_context(path, candidate)}: at least two coordinates are required; found {len(points)}."
        )
    return KmlTrack(
        points=points,
        geometry_kind=candidate.geometry_kind,
        placemark_name=candidate.placemark_name,
        altitude_mode=_altitude_mode(path, candidate, namespace),
    )


def parse_kml(file_path: str | os.PathLike[str]) -> list[tuple[float, float, float]]:
    """Compatibility adapter returning ``(lat, lon, altitude)`` tuples.

    New callers should use :func:`parse_kml_track`. Missing LineString altitude
    is represented as ``0.0`` here to preserve the historical tuple contract.
    """
    track = parse_kml_track(file_path)
    return [
        (
            point.latitude,
            point.longitude,
            point.altitude_m if point.altitude_m is not None else 0.0,
        )
        for point in track.points
    ]


def load_last_two_points_from_kml(
    input_file: str | os.PathLike[str],
) -> tuple[float, float, float, float, float]:
    """Compatibility adapter returning the historical debris tuple."""
    track = parse_kml_track(input_file)
    penultimate, final = track.points[-2:]
    return (
        penultimate.latitude,
        penultimate.longitude,
        final.latitude,
        final.longitude,
        final.altitude_m if final.altitude_m is not None else 0.0,
    )
