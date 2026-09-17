"""Conservative, source-preserving formatting for KML coordinate text."""

from __future__ import annotations

from dataclasses import dataclass
import re

from .kml_coordinates import inspect_line_string_coordinate
from .kml_file_handling import GX_NAMESPACE, SUPPORTED_KML_NAMESPACES
from .kml_source import XmlSourceNode, indent_from_gap, line_indent, scan_xml_source


@dataclass(frozen=True, slots=True)
class KmlCoordinateFormatResult:
    contents: str
    changed: bool = False
    valid_coordinate_count: int = 0
    invalid_token_count: int = 0
    gx_coordinate_count: int = 0
    message: str = ""


@dataclass(frozen=True, slots=True)
class _Replacement:
    start: int
    end: int
    value: bytes


def _format_coordinate_body(
    data: bytes,
    node: XmlSourceNode,
) -> tuple[_Replacement | None, int, int]:
    if node.close_start is None:
        return None, 0, 0
    body = data[node.content_start:node.close_start]
    # Markup and entity references require decoded-to-source offset mapping.
    # Leave those uncommon regions untouched rather than risk regrouping a
    # token differently from the XML parser.
    if b"<" in body or b"&" in body:
        return None, 0, 0
    matches = list(re.finditer(rb"\S+", body))
    if not matches:
        return None, 0, 0

    lines: list[list[bytes]] = []
    pending_invalid: list[bytes] = []
    valid_count = 0
    invalid_count = 0
    for match in matches:
        token = match.group()
        try:
            decoded = token.decode("utf-8")
        except UnicodeDecodeError:
            decoded = ""
        if decoded and inspect_line_string_coordinate(decoded).valid:
            lines.append([*pending_invalid, token])
            pending_invalid.clear()
            valid_count += 1
        else:
            pending_invalid.append(token)
            invalid_count += 1

    if pending_invalid:
        if lines:
            lines[-1].extend(pending_invalid)
        else:
            lines.append(list(pending_invalid))

    element_indent = line_indent(data, node.start)
    coordinate_indent = indent_from_gap(
        body[:matches[0].start()],
        element_indent + b"    ",
    )
    rendered_lines = [b" ".join(line) for line in lines]
    replacement = (
        b"\n"
        + coordinate_indent
        + (b"\n" + coordinate_indent).join(rendered_lines)
        + b"\n"
        + element_indent
    )
    if replacement == body:
        return None, valid_count, invalid_count
    return (
        _Replacement(node.content_start, node.close_start, replacement),
        valid_count,
        invalid_count,
    )


def _format_gx_track_gaps(data: bytes, node: XmlSourceNode) -> tuple[list[_Replacement], int]:
    if node.close_start is None:
        return [], 0
    coordinates = [
        (index, child)
        for index, child in enumerate(node.children)
        if child.namespace == GX_NAMESPACE and child.local_name == "coord"
    ]
    if not coordinates:
        return [], 0

    track_indent = line_indent(data, node.start)
    first_index, first_coord = coordinates[0]
    prior_end = (
        node.children[first_index - 1].end
        if first_index > 0
        else node.content_start
    )
    if prior_end is None:
        return [], 0
    first_gap = data[prior_end:first_coord.start]
    child_indent = indent_from_gap(first_gap, track_indent + b"    ")

    gaps: dict[tuple[int, int], bytes] = {}
    for index, coordinate in coordinates:
        before = node.children[index - 1].end if index > 0 else node.content_start
        after = (
            node.children[index + 1].start
            if index + 1 < len(node.children)
            else node.close_start
        )
        if before is None or coordinate.end is None:
            return [], 0
        before_gap = data[before:coordinate.start]
        after_gap = data[coordinate.end:after]
        if before_gap.strip() or after_gap.strip():
            return [], 0
        gaps[(before, coordinate.start)] = b"\n" + child_indent
        following_indent = track_indent if index + 1 == len(node.children) else child_indent
        gaps[(coordinate.end, after)] = b"\n" + following_indent

    replacements = [
        _Replacement(start, end, value)
        for (start, end), value in gaps.items()
        if data[start:end] != value
    ]
    return replacements, len(coordinates)


def format_kml_coordinates(contents: str) -> KmlCoordinateFormatResult:
    """Put supported KML coordinates on readable source lines without changing tokens."""
    if not contents:
        return KmlCoordinateFormatResult(contents, message="The document is empty.")

    data = contents.encode("utf-8")
    try:
        root, completed = scan_xml_source(contents)
    except Exception:
        return KmlCoordinateFormatResult(
            contents,
            message="Coordinates were not changed because the XML structure is malformed.",
        )

    root_name = None if root is None else (root.namespace, root.local_name)
    if root_name is None or root_name[1] != "kml":
        return KmlCoordinateFormatResult(
            contents,
            message="Coordinates were not changed because the document has no KML root element.",
        )
    if root_name[0] not in SUPPORTED_KML_NAMESPACES:
        return KmlCoordinateFormatResult(
            contents,
            message="Coordinates were not changed because the KML namespace is unsupported.",
        )

    replacements: list[_Replacement] = []
    valid_count = 0
    invalid_count = 0
    gx_count = 0
    for node in completed:
        if (
            node.namespace == root_name[0]
            and node.local_name == "coordinates"
            and node.parent is not None
            and node.parent.namespace == root_name[0]
            and node.parent.local_name == "LineString"
        ):
            replacement, valid, invalid = _format_coordinate_body(data, node)
            valid_count += valid
            invalid_count += invalid
            if replacement is not None:
                replacements.append(replacement)
        elif (
            node.namespace == GX_NAMESPACE
            and node.local_name == "Track"
            and node.parent is not None
            and node.parent.namespace == root_name[0]
            and node.parent.local_name == "Placemark"
        ):
            gx_replacements, count = _format_gx_track_gaps(data, node)
            replacements.extend(gx_replacements)
            gx_count += count

    replacements.sort(key=lambda value: (value.start, value.end))
    if any(
        first.end > second.start
        for first, second in zip(replacements, replacements[1:])
    ):
        return KmlCoordinateFormatResult(
            contents,
            message="Coordinates were not changed because their source spans overlap.",
        )

    rendered = data
    for replacement in reversed(replacements):
        rendered = rendered[:replacement.start] + replacement.value + rendered[replacement.end:]
    formatted = rendered.decode("utf-8")
    if formatted == contents:
        if valid_count or invalid_count or gx_count:
            message = "Coordinates are already arranged one per line."
        else:
            message = "No safely formattable coordinate values were found."
    else:
        total = valid_count + gx_count
        message = f"Arranged {total} coordinate{'s' if total != 1 else ''} one per line."
        if invalid_count:
            message += (
                f" Kept {invalid_count} invalid token{'s' if invalid_count != 1 else ''} "
                "beside a valid coordinate where possible."
            )
    return KmlCoordinateFormatResult(
        formatted,
        changed=formatted != contents,
        valid_coordinate_count=valid_count,
        invalid_token_count=invalid_count,
        gx_coordinate_count=gx_count,
        message=message,
    )


__all__ = ["KmlCoordinateFormatResult", "format_kml_coordinates"]
