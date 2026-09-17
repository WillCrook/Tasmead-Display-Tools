"""Pure crop, RDP and comparison-preview operations for the KML editor."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from typing import Callable, Iterable

import numpy as np
from dateutil.parser import isoparse

from .geodesy import LocalEnuFrame, inverse_distance_bearing
from .kml_export import (
    KmlCoordinate,
    KmlDocument,
    KmlLineString,
    KmlPlacemark,
    KmlStyle,
)
from .kml_file_handling import KmlPoint, KmlSourceSpan, KmlTrack
from .map_preview import PreparedTrace, PreviewScene


MAX_ENU_SECTION_LENGTH_M = 50_000.0
CancellationCheck = Callable[[], bool] | None


class KmlEditorOperationCancelled(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TimestampInfo:
    reliable: bool
    instants: tuple[datetime, ...] = ()
    reason: str = ""


@dataclass(frozen=True, slots=True)
class SimplificationResult:
    kept_indices: tuple[int, ...]
    original_count: int
    tolerance_m: float
    timestamps_reliable: bool

    @property
    def result_count(self) -> int:
        return len(self.kept_indices)

    @property
    def reduction_percent(self) -> float:
        if not self.original_count:
            return 0.0
        return 100.0 * (self.original_count - self.result_count) / self.original_count


@dataclass(frozen=True, slots=True)
class SourceEditResult:
    contents: str
    warnings: tuple[str, ...] = ()


def _cancelled(cancellation_check: CancellationCheck) -> None:
    if cancellation_check is not None and cancellation_check():
        raise KmlEditorOperationCancelled("The KML editor operation was cancelled.")


def timestamp_info(track: KmlTrack) -> TimestampInfo:
    if track.geometry_kind != "gx_track":
        return TimestampInfo(False, reason="This geometry has no aligned gx timestamps.")
    values = tuple(point.timestamp for point in track.points)
    if not values or any(value is None for value in values):
        return TimestampInfo(False, reason="Timestamps are missing or are not aligned with every point.")
    try:
        parsed = tuple(isoparse(value) for value in values if value is not None)
    except (TypeError, ValueError, OverflowError):
        return TimestampInfo(False, reason="Timestamps are not consistently parseable.")
    if any(value.tzinfo is None for value in parsed):
        return TimestampInfo(False, reason="Timestamps do not all include a time-zone offset.")
    if any(first >= second for first, second in zip(parsed, parsed[1:])):
        return TimestampInfo(False, reason="Timestamps are not strictly increasing.")
    return TimestampInfo(True, parsed)


def _validate_kept_indices(track: KmlTrack, kept_indices: Iterable[int]) -> tuple[int, ...]:
    kept = tuple(int(index) for index in kept_indices)
    if len(kept) < 2 or kept != tuple(sorted(set(kept))):
        raise ValueError("At least two unique retained points are required.")
    if kept[0] < 0 or kept[-1] >= len(track.points):
        raise ValueError("Retained point indexes are outside the current track.")
    return kept


def crop_indices(track: KmlTrack, start_index: int, end_index: int) -> tuple[int, ...]:
    start = int(start_index)
    end = int(end_index)
    if not 0 <= start < end < len(track.points):
        raise ValueError("Crop range must retain at least two current track points.")
    return tuple(range(start, end + 1))


def _forced_boundaries(
    track: KmlTrack,
    cancellation_check: CancellationCheck,
) -> tuple[int, ...]:
    forced = {0, len(track.points) - 1}
    section_distance = 0.0
    for index in range(1, len(track.points)):
        _cancelled(cancellation_check)
        first = track.points[index - 1]
        second = track.points[index]
        distance, _ = inverse_distance_bearing(
            first.latitude,
            first.longitude,
            second.latitude,
            second.longitude,
        )
        section_distance += distance
        if section_distance >= MAX_ENU_SECTION_LENGTH_M:
            forced.add(index)
            section_distance = 0.0
        if (first.altitude_m is None) != (second.altitude_m is None):
            forced.update((index - 1, index))
            section_distance = 0.0
    return tuple(sorted(forced))


def _rdp_section(
    track: KmlTrack,
    start_index: int,
    end_index: int,
    tolerance_m: float,
    instants: tuple[datetime, ...],
    cancellation_check: CancellationCheck,
) -> set[int]:
    if end_index <= start_index + 1:
        return {start_index, end_index}
    anchor = track.points[(start_index + end_index) // 2]
    frame = LocalEnuFrame(anchor.latitude, anchor.longitude)
    points = track.points[start_index : end_index + 1]
    horizontal = frame.to_enu_many(
        (point.latitude, point.longitude) for point in points
    )
    altitude_active = (
        track.altitude_mode in {"absolute", "relativeToGround"}
        and all(point.altitude_m is not None for point in points)
    )
    vectors = np.asarray(
        [
            (
                (position.east_m, position.north_m, float(point.altitude_m))
                if altitude_active
                else (position.east_m, position.north_m)
            )
            for position, point in zip(horizontal, points, strict=True)
        ],
        dtype=float,
    )
    keep = {0, len(vectors) - 1}
    stack = [(0, len(vectors) - 1)]
    while stack:
        _cancelled(cancellation_check)
        first_index, last_index = stack.pop()
        if last_index <= first_index + 1:
            continue
        first = vectors[first_index]
        last = vectors[last_index]
        candidates = vectors[first_index + 1 : last_index]
        if not len(candidates):
            continue
        segment = last - first
        denominator = float(np.dot(segment, segment))
        if denominator == 0.0:
            deviations = np.linalg.norm(candidates - first, axis=1)
        else:
            fractions = np.clip(((candidates - first) @ segment) / denominator, 0.0, 1.0)
            projected = first + fractions[:, None] * segment
            deviations = np.linalg.norm(candidates - projected, axis=1)
        absolute_first = start_index + first_index
        absolute_last = start_index + last_index
        if instants:
            duration = (instants[absolute_last] - instants[absolute_first]).total_seconds()
            if duration > 0.0:
                time_fractions = np.asarray(
                    [
                        (instant - instants[absolute_first]).total_seconds() / duration
                        for instant in instants[absolute_first + 1 : absolute_last]
                    ],
                    dtype=float,
                )
                timed = first + time_fractions[:, None] * segment
                deviations = np.maximum(
                    deviations,
                    np.linalg.norm(candidates - timed, axis=1),
                )
        relative_index = int(np.argmax(deviations))
        maximum = float(deviations[relative_index])
        maximum_index = first_index + 1 + relative_index
        if maximum > tolerance_m:
            keep.add(maximum_index)
            stack.append((first_index, maximum_index))
            stack.append((maximum_index, last_index))
    return {start_index + index for index in keep}


def simplify_track(
    track: KmlTrack,
    tolerance_m: float,
    *,
    cancellation_check: CancellationCheck = None,
) -> SimplificationResult:
    """Reduce a path with a maximum source-vertex deviation in metres.

    Each section uses a WGS84 local-ENU frame and is forced to end before its
    travelled length exceeds 50 km. Horizontal deviation always participates.
    Explicit altitude participates for ``absolute`` and ``relativeToGround``;
    ``clampToGround`` is horizontal-only because terrain height is unavailable.
    Mixed 2D/3D transitions and endpoints are forced, while reliable aligned
    timestamps add deviation from time-synchronised endpoint interpolation.
    """
    tolerance = float(tolerance_m)
    if not math.isfinite(tolerance) or tolerance <= 0.0:
        raise ValueError("Maximum path deviation must be a positive finite distance in metres.")
    if len(track.points) < 2:
        raise ValueError("A flight path requires at least two points.")
    timing = timestamp_info(track)
    instants = timing.instants if timing.reliable else ()
    boundaries = _forced_boundaries(track, cancellation_check)
    keep: set[int] = set(boundaries)
    for start, end in zip(boundaries, boundaries[1:]):
        keep.update(
            _rdp_section(
                track,
                start,
                end,
                tolerance,
                instants,
                cancellation_check,
            )
        )
    _cancelled(cancellation_check)
    return SimplificationResult(
        tuple(sorted(keep)),
        len(track.points),
        tolerance,
        timing.reliable,
    )


def _line_separator(data: bytes, spans: tuple[KmlSourceSpan, ...]) -> bytes:
    if len(spans) < 2:
        return b" "
    gap = data[spans[0].end : spans[1].start]
    if not gap or gap.strip():
        return b" "
    if b"\n" not in gap:
        return b" "
    return b"\n" + gap.rsplit(b"\n", 1)[-1]


def apply_retained_indices(
    contents: str,
    track: KmlTrack,
    kept_indices: Iterable[int],
    *,
    cancellation_check: CancellationCheck = None,
) -> SourceEditResult:
    _cancelled(cancellation_check)
    kept = _validate_kept_indices(track, kept_indices)
    binding = track.source_binding
    if binding is None or binding.unsafe_reason:
        raise ValueError(
            binding.unsafe_reason
            if binding is not None and binding.unsafe_reason
            else "Apply is unavailable because source mapping is not available."
        )
    if len(binding.coordinate_spans) != len(track.points):
        raise ValueError("Apply is unavailable because source coordinates are not aligned.")
    if kept == tuple(range(len(track.points))):
        return SourceEditResult(contents, binding.warnings)
    data = contents.encode("utf-8")
    if binding.geometry_kind == "line_string":
        body = binding.coordinate_body_span
        if body is None:
            raise ValueError("The selected LineString body is not source-mapped.")
        spans = binding.coordinate_spans
        tokens = []
        for item_index, index in enumerate(kept):
            if item_index % 2048 == 0:
                _cancelled(cancellation_check)
            tokens.append(data[spans[index].start : spans[index].end])
        prefix = data[body.start : spans[0].start]
        suffix = data[spans[-1].end : body.end]
        replacement = prefix + _line_separator(data, spans).join(tokens) + suffix
        rendered = data[: body.start] + replacement + data[body.end :]
    else:
        removals: list[KmlSourceSpan] = []
        retained = set(kept)
        for spans in (
            binding.coordinate_spans,
            *(series.element_spans for series in binding.aligned_series),
        ):
            removals.extend(
                span for index, span in enumerate(spans) if index not in retained
            )
        ordered = sorted(removals, key=lambda value: value.start)
        if any(first.end > second.start for first, second in zip(ordered, ordered[1:])):
            raise ValueError("Apply is unavailable because mapped gx metadata spans overlap.")
        pieces: list[bytes] = []
        cursor = 0
        for index, span in enumerate(ordered):
            if index % 2048 == 0:
                _cancelled(cancellation_check)
            pieces.append(data[cursor : span.start])
            cursor = span.end
        pieces.append(data[cursor:])
        rendered = b"".join(pieces)
    _cancelled(cancellation_check)
    return SourceEditResult(rendered.decode("utf-8"), binding.warnings)


def _coordinates(
    points: Iterable[KmlPoint],
    cancellation_check: CancellationCheck,
) -> tuple[KmlCoordinate, ...]:
    rendered = []
    for index, point in enumerate(points):
        if index % 2048 == 0:
            _cancelled(cancellation_check)
        rendered.append(
            KmlCoordinate(
                longitude=point.longitude,
                latitude=point.latitude,
                altitude_m=0.0 if point.altitude_m is None else point.altitude_m,
            )
        )
    return tuple(rendered)


def _preview_scene(
    track: KmlTrack,
    *,
    trace_id: str,
    label: str,
    geometries: tuple[tuple[str, str, float, tuple[KmlPoint, ...]], ...],
    cancellation_check: CancellationCheck = None,
) -> PreviewScene:
    styles = tuple(
        KmlStyle(style_id, colour, width) for style_id, colour, width, _points in geometries
    )
    placemarks = tuple(
        KmlPlacemark(
            name,
            f"#{style_id}",
            KmlLineString(_coordinates(points, cancellation_check), track.altitude_mode),
        )
        for (style_id, _colour, _width, points), name in zip(
            geometries,
            (item[0].replace("-", " ").title() for item in geometries),
            strict=True,
        )
        if len(points) >= 2
    )
    used_style_ids = {placemark.style_url[1:] for placemark in placemarks}
    styles = tuple(style for style in styles if style.style_id in used_style_ids)
    first = track.points[0]
    return PreviewScene(
        (
            PreparedTrace(
                trace_id=trace_id,
                label=label,
                anchor=KmlCoordinate(first.longitude, first.latitude, 0.0),
                base_document=KmlDocument(label, styles, placemarks),
                anchor_altitude_mode=track.altitude_mode,
            ),
        )
    )


def build_crop_preview_scene(
    track: KmlTrack,
    start_index: int,
    end_index: int,
    *,
    trace_id: str,
    label: str,
    cancellation_check: CancellationCheck = None,
) -> PreviewScene:
    kept = crop_indices(track, start_index, end_index)
    points = track.points
    geometries: list[tuple[str, str, float, tuple[KmlPoint, ...]]] = []
    if start_index > 0:
        geometries.append(("excluded-before", "80909090", 3.0, points[: start_index + 1]))
    geometries.append(
        ("retained", track.source_line_colour or "aaff00ff", 6.0, tuple(points[index] for index in kept))
    )
    if end_index < len(points) - 1:
        geometries.append(("excluded-after", "80909090", 3.0, points[end_index:]))
    return _preview_scene(
        track,
        trace_id=trace_id,
        label=label,
        geometries=tuple(geometries),
        cancellation_check=cancellation_check,
    )


def build_simplification_preview_scene(
    track: KmlTrack,
    result: SimplificationResult,
    *,
    trace_id: str,
    label: str,
    cancellation_check: CancellationCheck = None,
) -> PreviewScene:
    return _preview_scene(
        track,
        trace_id=trace_id,
        label=label,
        geometries=(
            ("original", "60909090", 2.0, track.points),
            (
                "simplified",
                track.source_line_colour or "aaff00ff",
                6.0,
                tuple(track.points[index] for index in result.kept_indices),
            ),
        ),
        cancellation_check=cancellation_check,
    )


__all__ = [
    "KmlEditorOperationCancelled",
    "MAX_ENU_SECTION_LENGTH_M",
    "SimplificationResult",
    "SourceEditResult",
    "TimestampInfo",
    "apply_retained_indices",
    "build_crop_preview_scene",
    "build_simplification_preview_scene",
    "crop_indices",
    "simplify_track",
    "timestamp_info",
]
