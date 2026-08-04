"""Shared, XML-safe KML 2.2 export support."""

from __future__ import annotations

import math
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


KML_NAMESPACE = "http://www.opengis.net/kml/2.2"
_STYLE_ID_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*\Z")
_COLOUR_RE = re.compile(r"[0-9a-fA-F]{8}\Z")
_ALTITUDE_MODES = frozenset({"absolute", "relativeToGround", "clampToGround"})


@dataclass(frozen=True, slots=True)
class KmlCoordinate:
    longitude: float
    latitude: float
    altitude_m: float


@dataclass(frozen=True, slots=True)
class KmlStyle:
    style_id: str
    line_colour: str
    line_width: float
    poly_colour: str | None = None


ATR_MAGENTA_TRACK_STYLE = KmlStyle(
    style_id="magentaTrackLine",
    line_colour="aaff00ff",
    line_width=6,
    poly_colour="33ff00ff",
)


@dataclass(frozen=True, slots=True)
class KmlLineString:
    coordinates: tuple[KmlCoordinate, ...]
    altitude_mode: str
    extrude_to_ground: bool = False
    tessellate: bool = False


@dataclass(frozen=True, slots=True)
class KmlPolygon:
    outer_ring: tuple[KmlCoordinate, ...]
    altitude_mode: str


KmlGeometry = KmlLineString | KmlPolygon


@dataclass(frozen=True, slots=True)
class KmlPlacemark:
    name: str
    style_url: str
    geometry: KmlGeometry


@dataclass(frozen=True, slots=True)
class KmlDocument:
    name: str | None
    styles: tuple[KmlStyle, ...]
    placemarks: tuple[KmlPlacemark, ...]


def _qualified(name: str) -> str:
    return f"{{{KML_NAMESPACE}}}{name}"


def _validate_xml_text(value: str, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be text.")
    for character in value:
        codepoint = ord(character)
        if not (
            codepoint in (0x09, 0x0A, 0x0D)
            or 0x20 <= codepoint <= 0xD7FF
            or 0xE000 <= codepoint <= 0xFFFD
            or 0x10000 <= codepoint <= 0x10FFFF
        ):
            raise ValueError(f"{field_name} contains a character forbidden by XML 1.0.")


def _validate_style(style: KmlStyle) -> None:
    _validate_xml_text(style.style_id, "style ID")
    if _STYLE_ID_RE.fullmatch(style.style_id) is None:
        raise ValueError(f'Invalid KML style ID "{style.style_id}".')
    for field_name, colour in (("line colour", style.line_colour), ("polygon colour", style.poly_colour)):
        if colour is not None and _COLOUR_RE.fullmatch(colour) is None:
            raise ValueError(f"{field_name} must be an eight-digit KML aabbggrr value.")
    if not math.isfinite(float(style.line_width)) or float(style.line_width) <= 0:
        raise ValueError("Line width must be a positive, finite number.")


def _validated_coordinate(coordinate: KmlCoordinate) -> tuple[float, float, float]:
    longitude = float(coordinate.longitude)
    latitude = float(coordinate.latitude)
    altitude = float(coordinate.altitude_m)
    if not all(math.isfinite(value) for value in (longitude, latitude, altitude)):
        raise ValueError("KML coordinates must be finite.")
    if not -180 <= longitude <= 180:
        raise ValueError("KML longitude must be within -180 to 180 degrees.")
    if not -90 <= latitude <= 90:
        raise ValueError("KML latitude must be within -90 to 90 degrees.")
    return longitude, latitude, altitude


def _format_number(value: float, places: int) -> str:
    rounded = round(value, places)
    if rounded == 0:
        rounded = 0.0
    return f"{rounded:.{places}f}"


def _coordinate_text(coordinates: Iterable[KmlCoordinate]) -> str:
    return "\n".join(
        ",".join(
            (
                _format_number(longitude, 7),
                _format_number(latitude, 7),
                _format_number(altitude, 3),
            )
        )
        for longitude, latitude, altitude in map(_validated_coordinate, coordinates)
    )


def _validate_altitude_mode(altitude_mode: str, *, extrude: bool) -> None:
    if altitude_mode not in _ALTITUDE_MODES:
        raise ValueError(f'Unsupported KML altitude mode "{altitude_mode}".')
    if extrude and altitude_mode == "clampToGround":
        raise ValueError("A clampToGround line cannot be extended to the ground.")


def _validated_line(line: KmlLineString) -> tuple[KmlCoordinate, ...]:
    _validate_altitude_mode(line.altitude_mode, extrude=line.extrude_to_ground)
    if len(line.coordinates) < 2:
        raise ValueError("A KML LineString requires at least two coordinates.")
    for coordinate in line.coordinates:
        _validated_coordinate(coordinate)
    return line.coordinates


def _validated_ring(polygon: KmlPolygon) -> tuple[KmlCoordinate, ...]:
    _validate_altitude_mode(polygon.altitude_mode, extrude=False)
    points = polygon.outer_ring
    if len(points) < 3:
        raise ValueError("A KML LinearRing requires at least three vertices.")
    for coordinate in points:
        _validated_coordinate(coordinate)
    if points[0] != points[-1]:
        points = (*points, points[0])
    if len(points) < 4:
        raise ValueError("A KML LinearRing requires at least four coordinates including closure.")
    return points


def _validate_document(document: KmlDocument) -> set[str]:
    if document.name is not None:
        _validate_xml_text(document.name, "document name")
    style_ids: set[str] = set()
    for style in document.styles:
        _validate_style(style)
        if style.style_id in style_ids:
            raise ValueError(f'Duplicate KML style ID "{style.style_id}".')
        style_ids.add(style.style_id)
    for placemark in document.placemarks:
        _validate_xml_text(placemark.name, "placemark name")
        if not placemark.style_url.startswith("#") or placemark.style_url[1:] not in style_ids:
            raise ValueError(f'Placemark "{placemark.name}" references an unknown KML style.')
        if isinstance(placemark.geometry, KmlLineString):
            _validated_line(placemark.geometry)
        elif isinstance(placemark.geometry, KmlPolygon):
            _validated_ring(placemark.geometry)
        else:
            raise TypeError("Unsupported KML geometry.")
    return style_ids


def _append_style(parent: ET.Element, style: KmlStyle) -> None:
    element = ET.SubElement(parent, _qualified("Style"), {"id": style.style_id})
    line_style = ET.SubElement(element, _qualified("LineStyle"))
    ET.SubElement(line_style, _qualified("color")).text = style.line_colour.lower()
    ET.SubElement(line_style, _qualified("width")).text = _format_number(float(style.line_width), 3).rstrip("0").rstrip(".")
    if style.poly_colour is not None:
        poly_style = ET.SubElement(element, _qualified("PolyStyle"))
        ET.SubElement(poly_style, _qualified("color")).text = style.poly_colour.lower()


def _append_geometry(parent: ET.Element, geometry: KmlGeometry) -> None:
    if isinstance(geometry, KmlLineString):
        line = ET.SubElement(parent, _qualified("LineString"))
        if geometry.extrude_to_ground:
            ET.SubElement(line, _qualified("extrude")).text = "1"
        ET.SubElement(line, _qualified("tessellate")).text = "1" if geometry.tessellate else "0"
        ET.SubElement(line, _qualified("altitudeMode")).text = geometry.altitude_mode
        ET.SubElement(line, _qualified("coordinates")).text = _coordinate_text(_validated_line(geometry))
        return

    polygon = ET.SubElement(parent, _qualified("Polygon"))
    ET.SubElement(polygon, _qualified("altitudeMode")).text = geometry.altitude_mode
    outer_boundary = ET.SubElement(polygon, _qualified("outerBoundaryIs"))
    ring = ET.SubElement(outer_boundary, _qualified("LinearRing"))
    ET.SubElement(ring, _qualified("coordinates")).text = _coordinate_text(_validated_ring(geometry))


def render_kml(document: KmlDocument) -> str:
    """Render a validated KML 2.2 document without writing it to disk."""
    _validate_document(document)
    ET.register_namespace("", KML_NAMESPACE)
    root = ET.Element(_qualified("kml"))
    document_element = ET.SubElement(root, _qualified("Document"))
    if document.name is not None:
        ET.SubElement(document_element, _qualified("name")).text = document.name
    for style in document.styles:
        _append_style(document_element, style)
    for placemark in document.placemarks:
        placemark_element = ET.SubElement(document_element, _qualified("Placemark"))
        ET.SubElement(placemark_element, _qualified("name")).text = placemark.name
        ET.SubElement(placemark_element, _qualified("styleUrl")).text = placemark.style_url
        _append_geometry(placemark_element, placemark.geometry)
    return ET.tostring(root, encoding="unicode", xml_declaration=True, short_empty_elements=False) + "\n"


def _exported_coordinates(document: KmlDocument) -> Iterable[KmlCoordinate]:
    for placemark in document.placemarks:
        geometry = placemark.geometry
        if isinstance(geometry, KmlLineString):
            yield from geometry.coordinates
        else:
            yield from _validated_ring(geometry)


def _raise_if_cancelled(cancellation_check: Callable[[], bool] | None) -> None:
    if cancellation_check is not None and cancellation_check():
        raise RuntimeError("KML export cancelled.")


def export_kml(
    file_path: str | os.PathLike[str],
    document: KmlDocument,
    *,
    overwrite: bool,
    cancellation_check: Callable[[], bool] | None = None,
    coordinate_callback: Callable[[], None] | None = None,
) -> None:
    """Atomically publish a rendered KML file in its destination directory.

    ``overwrite=False`` creates the destination atomically and raises
    :class:`FileExistsError` if another writer has already claimed it.
    """
    rendered = render_kml(document)
    _raise_if_cancelled(cancellation_check)
    for _ in _exported_coordinates(document):
        if coordinate_callback is not None:
            coordinate_callback()
        _raise_if_cancelled(cancellation_check)

    destination = Path(file_path)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(rendered)
            output.flush()
            os.fsync(output.fileno())
        _raise_if_cancelled(cancellation_check)
        if overwrite:
            os.replace(temporary_path, destination)
        else:
            os.link(temporary_path, destination)
            temporary_path.unlink()
    except BaseException:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise
