"""Strict, shared parsing for KML flight paths."""

from __future__ import annotations

from enum import Enum
from itertools import chain
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Literal
import xml.etree.ElementTree as ET

from .kml_coordinates import (
    CoordinateTokenInspection,
    CoordinateTokenIssue,
    inspect_gx_coordinate,
    inspect_line_string_coordinate,
)
from .kml_source import has_ancestor, scan_xml_source


KML_NAMESPACE = "http://www.opengis.net/kml/2.2"
LEGACY_GOOGLE_KML_NAMESPACE = "http://earth.google.com/kml/2.1"
GX_NAMESPACE = "http://www.google.com/kml/ext/2.2"
SUPPORTED_KML_NAMESPACES = ("", KML_NAMESPACE, LEGACY_GOOGLE_KML_NAMESPACE)
_COLOUR_RE = re.compile(r"[0-9a-fA-F]{8}\Z")


class KmlDiagnosticSeverity(str, Enum):
    ERROR = "error"


class KmlDiagnosticCode(str, Enum):
    XML_MALFORMED = "xml_malformed"
    ROOT_ELEMENT = "root_element"
    NAMESPACE_UNSUPPORTED = "namespace_unsupported"
    STRUCTURE_UNSUPPORTED = "structure_unsupported"
    GEOMETRY_MISSING = "geometry_missing"
    GEOMETRY_AMBIGUOUS = "geometry_ambiguous"
    COORDINATES_EMPTY = "coordinates_empty"
    COORDINATE_ARITY = "coordinate_arity"
    COORDINATE_NON_NUMERIC = "coordinate_non_numeric"
    COORDINATE_NON_FINITE = "coordinate_non_finite"
    COORDINATE_OUT_OF_RANGE = "coordinate_out_of_range"


@dataclass(frozen=True, slots=True)
class KmlSourceLocation:
    """A reliable selection in normalized editor text."""

    line: int
    column: int
    offset: int | None = None
    length: int = 0

    @property
    def display(self) -> str:
        return f"Line {self.line}, column {self.column}"


@dataclass(frozen=True, slots=True)
class KmlDiagnostic:
    code: KmlDiagnosticCode
    severity: KmlDiagnosticSeverity
    message: str
    explanation: str
    suggestion: str
    location: KmlSourceLocation | None = None


class KmlParseError(ValueError):
    """Base class for user-correctable KML parsing failures."""

    def __init__(self, message: str, diagnostic: KmlDiagnostic | None = None):
        super().__init__(message)
        self.diagnostic = diagnostic or KmlDiagnostic(
            code=KmlDiagnosticCode.STRUCTURE_UNSUPPORTED,
            severity=KmlDiagnosticSeverity.ERROR,
            message=message,
            explanation=message,
            suggestion="Inspect the KML structure and correct the reported problem.",
        )


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
class KmlSourceSpan:
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class KmlAlignedSourceSeries:
    label: str
    element_spans: tuple[KmlSourceSpan, ...]


@dataclass(frozen=True, slots=True)
class KmlTrackSourceBinding:
    """Exact UTF-8 byte spans for the parser-selected flight path."""

    geometry_kind: Literal["line_string", "gx_track"]
    coordinate_spans: tuple[KmlSourceSpan, ...]
    coordinate_body_span: KmlSourceSpan | None = None
    aligned_series: tuple[KmlAlignedSourceSeries, ...] = ()
    companion_point_count: int = 0
    warnings: tuple[str, ...] = ()
    unsafe_reason: str | None = None


@dataclass(frozen=True, slots=True)
class KmlTrack:
    """The single flight-path geometry selected from a KML document."""

    points: tuple[KmlPoint, ...]
    geometry_kind: Literal["line_string", "gx_track"]
    placemark_name: str | None
    altitude_mode: str = "clampToGround"
    source_line_colour: str | None = None
    source_binding: KmlTrackSourceBinding | None = field(
        default=None,
        compare=False,
        repr=False,
    )


@dataclass(frozen=True, slots=True)
class _TrackCandidate:
    element: ET.Element
    placemark: ET.Element
    geometry_kind: Literal["line_string", "gx_track"]
    placemark_name: str | None


def _location_at(text: str | None, offset: int, length: int = 0) -> KmlSourceLocation | None:
    if text is None or offset < 0 or offset > len(text):
        return None
    line_start = text.rfind("\n", 0, offset) + 1
    return KmlSourceLocation(
        line=text.count("\n", 0, offset) + 1,
        column=offset - line_start + 1,
        offset=offset,
        length=max(0, length),
    )


def _location_from_line_column(
    text: str | None,
    line: int | None,
    column: int | None,
) -> KmlSourceLocation | None:
    if text is None or line is None or column is None or line < 1 or column < 1:
        return None
    lines = text.splitlines(keepends=True)
    if line > len(lines):
        return None
    line_text = lines[line - 1].rstrip("\r\n")
    safe_column = min(column, len(line_text) + 1)
    offset = sum(len(value) for value in lines[: line - 1]) + safe_column - 1
    return KmlSourceLocation(line=line, column=safe_column, offset=offset, length=0)


def _unique_text_location(text: str | None, value: str) -> KmlSourceLocation | None:
    offset = _unique_text_offset(text, value)
    if offset is None:
        return None
    return _location_at(text, offset, len(value))


def _unique_text_offset(text: str | None, value: str | None) -> int | None:
    if text is None or not value:
        return None
    offset = text.find(value)
    if offset < 0 or text.find(value, offset + len(value)) >= 0:
        return None
    return offset


def _element_location(text: str | None, local_name: str) -> KmlSourceLocation | None:
    if text is None:
        return None
    pattern = re.compile(
        rf"<\s*(?:(?:[A-Za-z_][\w.-]*):)?{re.escape(local_name)}(?=\s|/?>)"
    )
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        return None
    name_offset = matches[0].start() + 1
    while name_offset < matches[0].end() and text[name_offset].isspace():
        name_offset += 1
    return _location_at(text, name_offset, matches[0].end() - name_offset)


def _error(
    error_type: type[KmlParseError],
    message: str,
    *,
    code: KmlDiagnosticCode,
    summary: str,
    explanation: str,
    suggestion: str,
    location: KmlSourceLocation | None = None,
) -> KmlParseError:
    return error_type(
        message,
        KmlDiagnostic(
            code=code,
            severity=KmlDiagnosticSeverity.ERROR,
            message=summary,
            explanation=explanation,
            suggestion=suggestion,
            location=location,
        ),
    )


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


def _direct_children(element: ET.Element, tag: str) -> list[ET.Element]:
    return [child for child in element if child.tag == tag]


def _style_line_colour(
    style: ET.Element,
    namespace: str,
) -> tuple[bool, str | None]:
    """Return whether a line colour is declared and its deterministic value."""
    line_styles = _direct_children(style, _qualified(namespace, "LineStyle"))
    if not line_styles:
        return False, None
    if len(line_styles) != 1:
        return True, None

    line_style = line_styles[0]
    colours = _direct_children(line_style, _qualified(namespace, "color"))
    if not colours:
        return False, None
    if len(colours) != 1:
        return True, None

    colour_modes = _direct_children(line_style, _qualified(namespace, "colorMode"))
    if len(colour_modes) > 1:
        return True, None
    if colour_modes:
        mode = (colour_modes[0].text or "").strip()
        if mode != "normal":
            return True, None

    colour = (colours[0].text or "").strip()
    if _COLOUR_RE.fullmatch(colour) is None:
        return True, None
    return True, colour.lower()


def _style_selectors_by_id(
    root: ET.Element,
    namespace: str,
) -> dict[str, ET.Element | None]:
    selector_tags = {
        _qualified(namespace, "Style"),
        _qualified(namespace, "StyleMap"),
    }
    selectors: dict[str, ET.Element | None] = {}
    for element in root.iter():
        if element.tag not in selector_tags:
            continue
        style_id = element.get("id")
        if not style_id:
            continue
        selectors[style_id] = element if style_id not in selectors else None
    return selectors


def _local_style_id(style_url: ET.Element | None) -> str | None:
    value = (style_url.text or "").strip() if style_url is not None else ""
    if not value.startswith("#") or len(value) == 1 or "#" in value[1:]:
        return None
    return value[1:]


def _resolve_style_selector_colour(
    selector: ET.Element,
    namespace: str,
    selectors: dict[str, ET.Element | None],
    visited: frozenset[str],
) -> str | None:
    _, local_name = _split_tag(selector.tag)
    if local_name == "Style":
        _, colour = _style_line_colour(selector, namespace)
        return colour
    if local_name != "StyleMap":
        return None

    normal_pairs = []
    for pair in _direct_children(selector, _qualified(namespace, "Pair")):
        keys = _direct_children(pair, _qualified(namespace, "key"))
        if len(keys) == 1 and (keys[0].text or "").strip() == "normal":
            normal_pairs.append(pair)
    if len(normal_pairs) != 1:
        return None

    pair = normal_pairs[0]
    inline_styles = _direct_children(pair, _qualified(namespace, "Style"))
    if len(inline_styles) > 1:
        return None
    if inline_styles:
        declared, colour = _style_line_colour(inline_styles[0], namespace)
        if declared:
            return colour

    style_urls = _direct_children(pair, _qualified(namespace, "styleUrl"))
    if len(style_urls) != 1:
        return None
    style_id = _local_style_id(style_urls[0])
    if style_id is None or style_id in visited:
        return None
    referenced = selectors.get(style_id)
    if referenced is None:
        return None
    return _resolve_style_selector_colour(
        referenced,
        namespace,
        selectors,
        visited | {style_id},
    )


def _placemark_line_colour(
    root: ET.Element,
    placemark: ET.Element,
    namespace: str,
) -> str | None:
    inline_styles = _direct_children(placemark, _qualified(namespace, "Style"))
    if len(inline_styles) > 1:
        return None
    if inline_styles:
        declared, colour = _style_line_colour(inline_styles[0], namespace)
        if declared:
            return colour

    style_urls = _direct_children(placemark, _qualified(namespace, "styleUrl"))
    if len(style_urls) != 1:
        return None
    style_id = _local_style_id(style_urls[0])
    if style_id is None:
        return None
    selectors = _style_selectors_by_id(root, namespace)
    selector = selectors.get(style_id)
    if selector is None:
        return None
    return _resolve_style_selector_colour(
        selector,
        namespace,
        selectors,
        frozenset({style_id}),
    )


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


def _make_point(
    inspection: CoordinateTokenInspection,
    path: Path,
    candidate: _TrackCandidate,
    tuple_index: int,
    source_text: str | None,
    location_offset: int | None,
    location_length: int,
) -> KmlPoint:
    location = None
    if inspection.issue is not None and location_offset is not None:
        location = _location_at(source_text, location_offset, location_length)
    if inspection.issue in {
        CoordinateTokenIssue.NON_NUMERIC,
        CoordinateTokenIssue.NON_FINITE,
    }:
        component = inspection.component or "coordinate"
        value = inspection.problem_value
        non_numeric = inspection.issue == CoordinateTokenIssue.NON_NUMERIC
        qualifier = "not numeric" if non_numeric else "not finite"
        message = f'{_context(path, candidate, tuple_index)}: {component} value "{value}" is {qualifier}.'
        raise _error(
            KmlCoordinateError,
            message,
            code=(
                KmlDiagnosticCode.COORDINATE_NON_NUMERIC
                if non_numeric
                else KmlDiagnosticCode.COORDINATE_NON_FINITE
            ),
            summary=(
                f"Coordinate {tuple_index} has a non-numeric {component}."
                if non_numeric
                else f"Coordinate {tuple_index} has a non-finite {component}."
            ),
            explanation=(
                f'The value "{value}" cannot be read as a finite decimal number in the '
                f"selected {candidate.geometry_kind.replace('_', ' ')}."
                if non_numeric
                else "KML coordinates must use finite decimal numbers; NaN and infinity are invalid."
            ),
            suggestion=(
                f"Replace the {component} with the intended decimal value."
                if non_numeric
                else f"Replace the {component} with the intended finite decimal value."
            ),
            location=location,
        )

    if inspection.issue in {
        CoordinateTokenIssue.LONGITUDE_OUT_OF_RANGE,
        CoordinateTokenIssue.LATITUDE_OUT_OF_RANGE,
    }:
        longitude_issue = inspection.issue == CoordinateTokenIssue.LONGITUDE_OUT_OF_RANGE
        component = "longitude" if longitude_issue else "latitude"
        value = inspection.problem_value
        supported_range = "-180 to 180" if longitude_issue else "-90 to 90"
        message = f"{_context(path, candidate, tuple_index)}: {component} {value} is outside {supported_range}."
        raise _error(
            KmlCoordinateError,
            message,
            code=KmlDiagnosticCode.COORDINATE_OUT_OF_RANGE,
            summary=f"Coordinate {tuple_index} has an out-of-range {component}.",
            explanation=f"{component.title()} {value} is outside the supported range {supported_range} degrees.",
            suggestion=f"Check longitude/latitude ordering and enter the intended {component}.",
            location=location,
        )

    if not inspection.valid or inspection.numbers is None:
        raise AssertionError("Coordinate arity must be checked before creating a KML point.")
    longitude, latitude = inspection.numbers[:2]
    altitude = inspection.numbers[2] if len(inspection.numbers) == 3 else None
    return KmlPoint(latitude=latitude, longitude=longitude, altitude_m=altitude)


def _parse_line_string(
    path: Path,
    candidate: _TrackCandidate,
    namespace: str,
    source_text: str | None,
) -> tuple[KmlPoint, ...]:
    coordinates_tag = _qualified(namespace, "coordinates")
    containers = [child for child in candidate.element if child.tag == coordinates_tag]
    if len(containers) != 1:
        message = f"{_context(path, candidate)}: expected exactly one coordinates element, found {len(containers)}."
        raise _error(
            KmlStructureError,
            message,
            code=KmlDiagnosticCode.STRUCTURE_UNSUPPORTED,
            summary="The LineString does not have exactly one coordinates element.",
            explanation=f"A supported LineString requires one coordinates element; {len(containers)} were found.",
            suggestion="Keep one coordinates element containing the intended flight path.",
            location=_element_location(source_text, "LineString"),
        )

    text = containers[0].text
    token_matches = re.finditer(r"\S+", text) if text else iter(())
    first_match = next(token_matches, None)
    if first_match is None:
        message = f"{_context(path, candidate)}: coordinates element is empty."
        raise _error(
            KmlCoordinateError,
            message,
            code=KmlDiagnosticCode.COORDINATES_EMPTY,
            summary="The coordinates element is empty.",
            explanation="The selected LineString contains no coordinate tuples.",
            suggestion="Add at least two longitude,latitude tuples and optional altitude values.",
            location=_element_location(source_text, "coordinates"),
        )

    container_offset = _unique_text_offset(source_text, text)
    points: list[KmlPoint] = []
    matches = chain((first_match,), token_matches)
    for index, match in enumerate(matches, start=1):
        token = match.group()
        location_offset = (
            container_offset + match.start() if container_offset is not None else None
        )
        inspection = inspect_line_string_coordinate(token)
        if inspection.issue == CoordinateTokenIssue.ARITY:
            message = (
                f'{_context(path, candidate, index)}: "{token}" must contain longitude,latitude '
                "and optional altitude."
            )
            raise _error(
                KmlCoordinateError,
                message,
                code=KmlDiagnosticCode.COORDINATE_ARITY,
                summary=f"Coordinate {index} has the wrong number of values.",
                explanation=(
                    f'The token "{token}" is not a complete KML coordinate. LineString '
                    "coordinates are whitespace-separated longitude,latitude tuples with an optional altitude."
                ),
                suggestion=(
                    "Inspect whether the token is unintended and should be removed, or complete it "
                    "with the intended longitude, latitude and optional altitude."
                ),
                location=(
                    _location_at(source_text, location_offset, len(token))
                    if location_offset is not None
                    else None
                ),
            )
        points.append(
            _make_point(
                inspection,
                path,
                candidate,
                index,
                source_text,
                location_offset,
                len(token),
            )
        )
    return tuple(points)


def _altitude_mode(
    path: Path,
    candidate: _TrackCandidate,
    namespace: str,
    source_text: str | None,
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
            message = f'{_context(path, candidate)}: unsupported altitude mode "{value}".'
            raise _error(
                KmlStructureError,
                message,
                code=KmlDiagnosticCode.STRUCTURE_UNSUPPORTED,
                summary="The geometry uses an unsupported altitude mode.",
                explanation=f'The altitude mode "{value}" is not supported by the shared KML parser.',
                suggestion="Use a standard KML altitudeMode value appropriate for the source data.",
                location=_unique_text_location(source_text, value),
            )
    return "clampToGround"


def _parse_gx_track(
    path: Path,
    candidate: _TrackCandidate,
    namespace: str,
    source_text: str | None,
) -> tuple[KmlPoint, ...]:
    coord_tag = _qualified(GX_NAMESPACE, "coord")
    elements = [child for child in candidate.element if child.tag == coord_tag]
    if not elements:
        message = f"{_context(path, candidate)}: gx:Track contains no gx:coord elements."
        raise _error(
            KmlCoordinateError,
            message,
            code=KmlDiagnosticCode.COORDINATES_EMPTY,
            summary="The gx:Track contains no coordinates.",
            explanation="A supported gx:Track requires at least two gx:coord elements.",
            suggestion="Add gx:coord elements containing longitude latitude altitude values.",
            location=_element_location(source_text, "Track"),
        )

    when_tags = {_qualified(namespace, "when")}
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
            message = (
                f"{_context(path, candidate, index)}: empty gx:coord values require "
                "interpolation, which is not supported."
            )
            raise _error(
                KmlCoordinateError,
                message,
                code=KmlDiagnosticCode.COORDINATES_EMPTY,
                summary=f"Coordinate {index} is empty.",
                explanation="Empty gx:coord values would require interpolation, which is not supported.",
                suggestion="Enter the intended longitude latitude altitude values.",
                location=None,
            )
        inspection = inspect_gx_coordinate(text)
        location_offset = (
            _unique_text_offset(source_text, text)
            if inspection.issue is not None
            else None
        )
        if inspection.issue == CoordinateTokenIssue.ARITY:
            message = f'{_context(path, candidate, index)}: "{text}" must contain longitude latitude altitude.'
            raise _error(
                KmlCoordinateError,
                message,
                code=KmlDiagnosticCode.COORDINATE_ARITY,
                summary=f"Coordinate {index} has the wrong number of values.",
                explanation="Each gx:coord must contain exactly longitude latitude altitude.",
                suggestion="Complete or replace the coordinate with exactly three decimal values.",
                location=(
                    _location_at(source_text, location_offset, len(text))
                    if location_offset is not None
                    else None
                ),
            )
        point = _make_point(
            inspection,
            path,
            candidate,
            index,
            source_text,
            location_offset,
            len(text),
        )
        points.append(
            KmlPoint(
                latitude=point.latitude,
                longitude=point.longitude,
                altitude_m=point.altitude_m,
                timestamp=timestamps[index - 1],
            )
        )
    return tuple(points)


def _parse_root(root: ET.Element, path: Path, source_text: str | None) -> KmlTrack:
    namespace, local_name = _split_tag(root.tag)
    if local_name != "kml":
        message = f'{path.name}: expected a kml root element, found "{local_name}".'
        raise _error(
            KmlStructureError,
            message,
            code=KmlDiagnosticCode.ROOT_ELEMENT,
            summary="The document root is not kml.",
            explanation=f'The root element is "{local_name}"; a KML document must start with kml.',
            suggestion="Check that the selected file is KML and use a kml root element.",
            location=_element_location(source_text, local_name),
        )
    if namespace not in SUPPORTED_KML_NAMESPACES:
        message = f'{path.name}: unsupported KML namespace "{namespace}".'
        raise _error(
            KmlStructureError,
            message,
            code=KmlDiagnosticCode.NAMESPACE_UNSUPPORTED,
            summary="The KML namespace is not supported.",
            explanation=(
                f'The root namespace is "{namespace}". Only namespace-free KML, OGC KML 2.2 '
                "and the explicit Google KML 2.1 namespace are supported."
            ),
            suggestion="Confirm the document's KML version and correct the root xmlns value if it is wrong.",
            location=_unique_text_location(source_text, namespace),
        )

    placemark_tag = _qualified(namespace, "Placemark")
    line_string_tag = _qualified(namespace, "LineString")
    gx_track_tag = _qualified(GX_NAMESPACE, "Track")
    candidates: list[_TrackCandidate] = []

    for placemark in root.iter(placemark_tag):
        name = _placemark_name(placemark, namespace)
        for element in placemark.iter():
            if element.tag == line_string_tag:
                candidates.append(_TrackCandidate(element, placemark, "line_string", name))
            elif element.tag == gx_track_tag:
                candidates.append(_TrackCandidate(element, placemark, "gx_track", name))

    if not candidates:
        message = f"{path.name}: no supported LineString or gx:Track flight path was found inside a Placemark."
        raise _error(
            KmlStructureError,
            message,
            code=KmlDiagnosticCode.GEOMETRY_MISSING,
            summary="No supported flight-path geometry was found.",
            explanation="The parser found no LineString or gx:Track inside a Placemark.",
            suggestion="Place exactly one intended LineString or gx:Track flight path inside a Placemark.",
            location=_element_location(source_text, "kml"),
        )
    if len(candidates) > 1:
        descriptions = [
            f'{candidate.placemark_name or "unnamed Placemark"} '
            f'({"LineString" if candidate.geometry_kind == "line_string" else "gx:Track"})'
            for candidate in candidates
        ]
        message = (
            f"{path.name}: found {len(candidates)} flight paths; exactly one is required: "
            + "; ".join(descriptions)
            + "."
        )
        first_geometry = "LineString" if candidates[0].geometry_kind == "line_string" else "Track"
        raise _error(
            KmlStructureError,
            message,
            code=KmlDiagnosticCode.GEOMETRY_AMBIGUOUS,
            summary=f"The document contains {len(candidates)} supported flight paths.",
            explanation="The parser cannot choose between: " + "; ".join(descriptions) + ".",
            suggestion="Keep only the intended flight path or move unrelated geometry outside its Placemark.",
            location=_element_location(source_text, first_geometry),
        )

    candidate = candidates[0]
    if candidate.geometry_kind == "line_string":
        points = _parse_line_string(path, candidate, namespace, source_text)
    else:
        points = _parse_gx_track(path, candidate, namespace, source_text)

    if len(points) < 2:
        message = f"{_context(path, candidate)}: at least two coordinates are required; found {len(points)}."
        raise _error(
            KmlStructureError,
            message,
            code=KmlDiagnosticCode.STRUCTURE_UNSUPPORTED,
            summary="The flight path has fewer than two coordinates.",
            explanation=f"A path needs at least two positions; this geometry contains {len(points)}.",
            suggestion="Add the missing intended coordinate or select a different flight-path geometry.",
            location=_element_location(
                source_text,
                "LineString" if candidate.geometry_kind == "line_string" else "Track",
            ),
        )
    return KmlTrack(
        points=points,
        geometry_kind=candidate.geometry_kind,
        placemark_name=candidate.placemark_name,
        altitude_mode=_altitude_mode(path, candidate, namespace, source_text),
        source_line_colour=_placemark_line_colour(root, candidate.placemark, namespace),
    )


def _xml_error(path: Path, error: ET.ParseError, source_text: str | None) -> KmlXmlError:
    line, zero_based_column = getattr(error, "position", (None, None))
    location_text = (
        f" at line {line}, column {zero_based_column}"
        if line is not None
        else ""
    )
    message = f"{path.name}: invalid XML{location_text}: {error}."
    one_based_column = zero_based_column + 1 if zero_based_column is not None else None
    diagnostic_location = _location_from_line_column(source_text, line, one_based_column)
    if diagnostic_location is None and line is not None and one_based_column is not None:
        diagnostic_location = KmlSourceLocation(line=line, column=one_based_column)
    return _error(
        KmlXmlError,
        message,
        code=KmlDiagnosticCode.XML_MALFORMED,
        summary="The XML is not well formed.",
        explanation=str(error),
        suggestion="Inspect the nearby opening, closing and quoted XML syntax and correct the mismatch.",
        location=diagnostic_location,
    )


def _source_span(node) -> KmlSourceSpan | None:
    if node.end is None:
        return None
    return KmlSourceSpan(node.start, node.end)


def _track_source_binding(
    contents: str,
    track: KmlTrack,
    cancellation_check: Callable[[], bool] | None = None,
) -> KmlTrackSourceBinding:
    """Bind the semantically selected track back to exact source bytes."""
    root, nodes = scan_xml_source(contents, cancellation_check=cancellation_check)
    if root is None:
        raise ValueError("The KML source has no root element.")
    namespace = root.namespace
    local_name = "LineString" if track.geometry_kind == "line_string" else "Track"
    geometry_namespace = namespace if track.geometry_kind == "line_string" else GX_NAMESPACE
    candidates = [
        node
        for node in nodes
        if node.namespace == geometry_namespace
        and node.local_name == local_name
        and has_ancestor(node, namespace=namespace, local_name="Placemark")
    ]
    if len(candidates) != 1:
        raise ValueError("The selected flight path could not be mapped uniquely to its source.")
    geometry = candidates[0]
    points = [
        node
        for node in nodes
        if node.namespace == namespace
        and node.local_name == "Point"
        and has_ancestor(node, namespace=namespace, local_name="Placemark")
    ]
    warnings: list[str] = []
    if points:
        warnings.append(
            f"Preserved {len(points)} Point feature{'s' if len(points) != 1 else ''}; "
            "their relationship to the flight path cannot be established safely."
        )

    if track.geometry_kind == "line_string":
        containers = [
            child
            for child in geometry.children
            if child.namespace == namespace and child.local_name == "coordinates"
        ]
        if len(containers) != 1 or containers[0].close_start is None:
            raise ValueError("The selected LineString coordinates could not be mapped safely.")
        container = containers[0]
        data = contents.encode("utf-8")
        body = data[container.content_start : container.close_start]
        unsafe_reason = None
        if b"<" in body or b"&" in body:
            unsafe_reason = (
                "Apply is unavailable because the selected coordinates use markup, "
                "CDATA or entity references that cannot be rewritten source-safely."
            )
        spans = tuple(
            KmlSourceSpan(
                container.content_start + match.start(),
                container.content_start + match.end(),
            )
            for match in re.finditer(rb"\S+", body)
        )
        return KmlTrackSourceBinding(
            geometry_kind=track.geometry_kind,
            coordinate_spans=spans,
            coordinate_body_span=KmlSourceSpan(
                container.content_start,
                container.close_start,
            ),
            companion_point_count=len(points),
            warnings=tuple(warnings),
            unsafe_reason=unsafe_reason,
        )

    coordinate_nodes = [
        child
        for child in geometry.children
        if child.namespace == GX_NAMESPACE and child.local_name == "coord"
    ]
    coordinate_spans = tuple(
        span for node in coordinate_nodes if (span := _source_span(node)) is not None
    )
    aligned: list[KmlAlignedSourceSeries] = []
    series_candidates: list[tuple[str, list]] = [
        (
            "timestamps",
            [
                child
                for child in geometry.children
                if child.namespace == namespace and child.local_name == "when"
            ],
        ),
        (
            "angles",
            [
                child
                for child in geometry.children
                if child.namespace == GX_NAMESPACE and child.local_name == "angles"
            ],
        ),
    ]
    def belongs_to_selected_geometry(node) -> bool:
        parent = node.parent
        while parent is not None:
            if parent is geometry:
                return True
            parent = parent.parent
        return False

    simple_arrays = [
        node
        for node in nodes
        if node.namespace == GX_NAMESPACE
        and node.local_name == "SimpleArrayData"
        and belongs_to_selected_geometry(node)
    ]
    for index, array in enumerate(simple_arrays, start=1):
        series_candidates.append(
            (
                f"extended data array {index}",
                [
                    child
                    for child in array.children
                    if child.namespace == GX_NAMESPACE and child.local_name == "value"
                ],
            )
        )
    for label, elements in series_candidates:
        spans = tuple(
            span for node in elements if (span := _source_span(node)) is not None
        )
        if len(spans) == len(coordinate_spans) and spans:
            aligned.append(KmlAlignedSourceSeries(label, spans))
        elif spans:
            warnings.append(
                f"Preserved unaligned {label}; {len(spans)} values do not match "
                f"{len(coordinate_spans)} flight-path points."
            )
    return KmlTrackSourceBinding(
        geometry_kind=track.geometry_kind,
        coordinate_spans=coordinate_spans,
        aligned_series=tuple(aligned),
        companion_point_count=len(points),
        warnings=tuple(warnings),
    )


def parse_kml_text(
    contents: str,
    *,
    source_name: str = "untitled.kml",
    cancellation_check: Callable[[], bool] | None = None,
) -> KmlTrack:
    """Parse current editor text through the shared KML semantic parser."""
    if not isinstance(contents, str):
        raise TypeError("contents must be text")
    path = Path(source_name)
    try:
        parser = ET.XMLParser()
        chunk_size = 256 * 1024
        for offset in range(0, len(contents), chunk_size):
            if cancellation_check is not None and cancellation_check():
                raise RuntimeError("KML validation was cancelled.")
            parser.feed(contents[offset : offset + chunk_size])
        root = parser.close()
    except ET.ParseError as error:
        raise _xml_error(path, error, contents) from error
    track = _parse_root(root, path, contents)
    try:
        binding = _track_source_binding(contents, track, cancellation_check)
    except Exception:
        if cancellation_check is not None and cancellation_check():
            raise RuntimeError("KML validation was cancelled.")
        binding = KmlTrackSourceBinding(
            geometry_kind=track.geometry_kind,
            coordinate_spans=(),
            unsafe_reason=(
                "Apply is unavailable because the selected flight path could not be "
                "mapped uniquely back to the source text."
            ),
        )
    return replace(track, source_binding=binding)


def parse_kml_track(file_path: str | os.PathLike[str]) -> KmlTrack:
    """Parse exactly one KML LineString or gx:Track into a shared track model.

    Altitudes are returned exactly as encoded. A two-dimensional LineString
    coordinate has ``altitude_m=None``; no altitude-mode or terrain conversion
    is performed.
    """
    path = Path(file_path)
    if path.suffix.lower() == ".kmz":
        message = f"{path.name}: KMZ archives are not supported; select a KML file."
        raise _error(
            KmlStructureError,
            message,
            code=KmlDiagnosticCode.STRUCTURE_UNSUPPORTED,
            summary="KMZ archives are not supported.",
            explanation="The shared parser accepts raw .kml XML documents, not compressed KMZ archives.",
            suggestion="Extract and select the intended .kml document.",
        )

    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as error:
        raise _xml_error(path, error, None) from error
    return _parse_root(root, path, None)


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
