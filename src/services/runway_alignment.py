"""Runway inference and geodesic helpers for flight-path transposition."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Sequence

from .kml_file_handling import KmlPoint, KmlTrack


EARTH_RADIUS_M = 6_371_008.8
JITTER_DISTANCE_M = 2.0
MIN_CANDIDATE_LENGTH_M = 400.0
PREFERRED_CANDIDATE_LENGTH_M = 800.0
MAX_CANDIDATE_LENGTH_M = 1_500.0
MIN_DEPARTURE_CLIMB_M = 15.0
MAX_EVENT_HEADING_DIFFERENCE_DEG = 8.0
GROUND_ELEVATION_SEARCH_DISTANCE_M = 500.0
GROUND_ELEVATION_CROSS_TRACK_M = 30.0
MIN_GROUND_ELEVATION_SAMPLES = 5
MIN_GROUND_ELEVATION_SPAN_M = 75.0
MAX_GROUND_ELEVATION_SPAN_M = 400.0
MAX_GROUND_ELEVATION_SPREAD_M = 4.0
MAX_GROUND_ELEVATION_SLOPE = 0.02
MIN_SLOPE_PAIR_DISTANCE_M = 10.0


class RunwayConfidence(str, Enum):
    """Human-facing confidence assigned to an inferred runway candidate."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True, slots=True)
class RunwayReference:
    """One directional runway threshold used as a transposition anchor."""

    latitude: float
    longitude: float
    true_heading_deg: float
    elevation_m: float | None = None

    def __post_init__(self) -> None:
        latitude = float(self.latitude)
        longitude = float(self.longitude)
        heading = float(self.true_heading_deg)
        if not all(math.isfinite(value) for value in (latitude, longitude, heading)):
            raise ValueError("Runway coordinates and heading must be finite numbers.")
        if not -90.0 <= latitude <= 90.0:
            raise ValueError("Runway latitude must be between -90 and 90 degrees.")
        if not -180.0 <= longitude <= 180.0:
            raise ValueError("Runway longitude must be between -180 and 180 degrees.")
        elevation = self.elevation_m
        if elevation is not None:
            elevation = float(elevation)
            if not math.isfinite(elevation):
                raise ValueError("Runway elevation must be a finite number.")
        object.__setattr__(self, "latitude", latitude)
        object.__setattr__(self, "longitude", longitude)
        object.__setattr__(self, "true_heading_deg", heading % 360.0)
        object.__setattr__(self, "elevation_m", elevation)


@dataclass(frozen=True, slots=True)
class RunwayCandidate:
    """An unconfirmed departure-runway alignment inferred from one track."""

    reference: RunwayReference
    heading_confidence: RunwayConfidence
    threshold_confidence: RunwayConfidence
    start_index: int
    end_index: int
    aligned_distance_m: float
    heading_dispersion_deg: float
    cross_track_error_m: float
    evidence: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    @property
    def confidence(self) -> RunwayConfidence:
        """Return the lower confidence for callers using the original API."""
        confidence_order = {
            RunwayConfidence.LOW: 0,
            RunwayConfidence.MEDIUM: 1,
            RunwayConfidence.HIGH: 2,
        }
        return min(
            (self.heading_confidence, self.threshold_confidence),
            key=confidence_order.__getitem__,
        )


@dataclass(frozen=True, slots=True)
class RunwayInferenceResult:
    """The best candidate plus evidence, or a reason manual entry is required."""

    candidate: RunwayCandidate | None
    warnings: tuple[str, ...] = ()
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _Sample:
    original_index: int
    point: KmlPoint
    east_m: float
    north_m: float


@dataclass(frozen=True, slots=True)
class _WindowFit:
    samples: tuple[_Sample, ...]
    heading_deg: float
    span_m: float
    path_m: float
    straightness: float
    cross_track_p95_m: float
    heading_dispersion_deg: float
    initial_altitude_m: float | None
    climb_m: float | None
    score: float


@dataclass(frozen=True, slots=True)
class _RunwayEvent:
    """Overlapping, similarly directed straight windows from one runway pass."""

    fits: tuple[_WindowFit, ...]
    best_fit: _WindowFit
    threshold: _Sample
    end_index: int
    maximum_climb_m: float | None
    minimum_climb_m: float | None
    backtracked_distance_m: float


@dataclass(frozen=True, slots=True)
class _GroundElevationSample:
    """One absolute-altitude sample projected onto the selected runway axis."""

    along_runway_m: float
    altitude_m: float
    original_index: int


@dataclass(frozen=True, slots=True)
class _GroundElevationEstimate:
    """Robust ground-reference elevation and the window supporting it."""

    elevation_m: float
    sample_count: int
    start_distance_m: float
    end_distance_m: float
    altitude_spread_m: float
    slope: float


def _angular_difference(left: float, right: float) -> float:
    return (left - right + 180.0) % 360.0 - 180.0


def inverse_distance_bearing(
    start_latitude: float,
    start_longitude: float,
    end_latitude: float,
    end_longitude: float,
) -> tuple[float, float]:
    """Return spherical surface distance in metres and initial true bearing."""
    lat1, lat2 = map(math.radians, (start_latitude, end_latitude))
    delta_lat = lat2 - lat1
    delta_lon = math.radians(end_longitude - start_longitude)
    haversine = (
        math.sin(delta_lat / 2.0) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2.0) ** 2
    )
    distance = 2.0 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(haversine)))
    y = math.sin(delta_lon) * math.cos(lat2)
    x = (
        math.cos(lat1) * math.sin(lat2)
        - math.sin(lat1) * math.cos(lat2) * math.cos(delta_lon)
    )
    bearing = math.degrees(math.atan2(y, x)) % 360.0 if distance else 0.0
    return distance, bearing


def destination_point(
    latitude: float,
    longitude: float,
    distance_m: float,
    true_bearing_deg: float,
) -> tuple[float, float]:
    """Project a point along a spherical geodesic."""
    angular_distance = float(distance_m) / EARTH_RADIUS_M
    bearing = math.radians(true_bearing_deg)
    latitude_r = math.radians(latitude)
    longitude_r = math.radians(longitude)
    final_latitude = math.asin(
        math.sin(latitude_r) * math.cos(angular_distance)
        + math.cos(latitude_r) * math.sin(angular_distance) * math.cos(bearing)
    )
    final_longitude = longitude_r + math.atan2(
        math.sin(bearing) * math.sin(angular_distance) * math.cos(latitude_r),
        math.cos(angular_distance) - math.sin(latitude_r) * math.sin(final_latitude),
    )
    normalized_longitude = (math.degrees(final_longitude) + 540.0) % 360.0 - 180.0
    return math.degrees(final_latitude), normalized_longitude


def transpose_geodesic_points(
    points: Iterable[tuple[float, float, float]],
    source_runway: RunwayReference,
    target_runway: RunwayReference,
) -> tuple[tuple[float, float, float], ...]:
    """Move and rotate points between directional runway thresholds."""
    heading_delta = target_runway.true_heading_deg - source_runway.true_heading_deg
    transformed: list[tuple[float, float, float]] = []
    for latitude, longitude, altitude in points:
        distance, bearing = inverse_distance_bearing(
            source_runway.latitude,
            source_runway.longitude,
            latitude,
            longitude,
        )
        final_latitude, final_longitude = destination_point(
            target_runway.latitude,
            target_runway.longitude,
            distance,
            bearing + heading_delta,
        )
        transformed.append((final_latitude, final_longitude, altitude))
    return tuple(transformed)


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return ordered[position]


def _local_samples(track: KmlTrack) -> tuple[list[_Sample], int]:
    origin = track.points[0]
    latitude_scale = math.pi * EARTH_RADIUS_M / 180.0
    longitude_scale = latitude_scale * math.cos(math.radians(origin.latitude))
    samples: list[_Sample] = []
    discarded = 0
    for index, point in enumerate(track.points):
        if samples:
            distance, _ = inverse_distance_bearing(
                samples[-1].point.latitude,
                samples[-1].point.longitude,
                point.latitude,
                point.longitude,
            )
            if distance < JITTER_DISTANCE_M:
                discarded += 1
                continue
        samples.append(
            _Sample(
                original_index=index,
                point=point,
                east_m=(point.longitude - origin.longitude) * longitude_scale,
                north_m=(point.latitude - origin.latitude) * latitude_scale,
            )
        )
    return samples, discarded


def _sample_distance(left: _Sample, right: _Sample) -> float:
    return math.hypot(right.east_m - left.east_m, right.north_m - left.north_m)


def _contiguous_groups(samples: Sequence[_Sample]) -> tuple[list[list[_Sample]], int]:
    distances = [
        _sample_distance(left, right)
        for left, right in zip(samples, samples[1:])
        if _sample_distance(left, right) > 0.0
    ]
    typical_step = statistics.median(distances) if distances else 0.0
    jump_limit = max(250.0, typical_step * 8.0)
    groups: list[list[_Sample]] = [[]]
    discontinuities = 0
    for sample in samples:
        if groups[-1] and _sample_distance(groups[-1][-1], sample) > jump_limit:
            discontinuities += 1
            groups.append([])
        groups[-1].append(sample)
    return [group for group in groups if group], discontinuities


def _fit_window(samples: Sequence[_Sample], start_order: int) -> _WindowFit | None:
    if len(samples) < 2:
        return None
    east_mean = statistics.fmean(sample.east_m for sample in samples)
    north_mean = statistics.fmean(sample.north_m for sample in samples)
    covariance_east = statistics.fmean(
        (sample.east_m - east_mean) ** 2 for sample in samples
    )
    covariance_north = statistics.fmean(
        (sample.north_m - north_mean) ** 2 for sample in samples
    )
    covariance_cross = statistics.fmean(
        (sample.east_m - east_mean) * (sample.north_m - north_mean)
        for sample in samples
    )
    angle = 0.5 * math.atan2(
        2.0 * covariance_cross,
        covariance_east - covariance_north,
    )
    east_axis, north_axis = math.cos(angle), math.sin(angle)
    chronological_dot = (
        (samples[-1].east_m - samples[0].east_m) * east_axis
        + (samples[-1].north_m - samples[0].north_m) * north_axis
    )
    if chronological_dot < 0.0:
        east_axis, north_axis = -east_axis, -north_axis

    projections = [
        (sample.east_m - east_mean) * east_axis
        + (sample.north_m - north_mean) * north_axis
        for sample in samples
    ]
    residuals = [
        abs(
            -(sample.east_m - east_mean) * north_axis
            + (sample.north_m - north_mean) * east_axis
        )
        for sample in samples
    ]
    span = max(projections) - min(projections)
    step_distances = [
        _sample_distance(left, right) for left, right in zip(samples, samples[1:])
    ]
    path = sum(step_distances)
    if path <= 0.0:
        return None
    heading = math.degrees(math.atan2(east_axis, north_axis)) % 360.0
    step_differences: list[float] = []
    for left, right, distance in zip(samples, samples[1:], step_distances):
        if distance < JITTER_DISTANCE_M:
            continue
        step_heading = math.degrees(
            math.atan2(right.east_m - left.east_m, right.north_m - left.north_m)
        ) % 360.0
        step_differences.append(abs(_angular_difference(step_heading, heading)))
    dispersion = statistics.median(step_differences) if step_differences else 180.0
    altitudes = [sample.point.altitude_m for sample in samples]
    valid_altitudes = [value for value in altitudes if value is not None]
    initial_altitude = None
    climb = None
    if len(valid_altitudes) >= 4:
        section = max(1, len(valid_altitudes) // 5)
        initial_altitude = statistics.median(valid_altitudes[:section])
        climb = statistics.median(valid_altitudes[-section:]) - initial_altitude
    elif valid_altitudes:
        initial_altitude = statistics.median(valid_altitudes)
    cross_track = _percentile(residuals, 0.95)
    straightness = min(1.0, span / path)
    score = (
        span
        + (
            150.0
            if climb is not None and climb >= MIN_DEPARTURE_CLIMB_M
            else 0.0
        )
        - cross_track * 8.0
        - dispersion * 6.0
        - start_order * 0.05
    )
    return _WindowFit(
        samples=tuple(samples),
        heading_deg=heading,
        span_m=span,
        path_m=path,
        straightness=straightness,
        cross_track_p95_m=cross_track,
        heading_dispersion_deg=dispersion,
        initial_altitude_m=initial_altitude,
        climb_m=climb,
        score=score,
    )


def _candidate_windows(
    groups: Sequence[Sequence[_Sample]],
) -> list[_WindowFit]:
    """Fit every sustained straight segment; event classification follows later."""
    fits: list[_WindowFit] = []
    for group in groups:
        if len(group) < 2:
            continue
        step = max(1, len(group) // 350)
        for start in range(0, len(group) - 1, step):
            path = 0.0
            end = start
            while end + 1 < len(group) and path < PREFERRED_CANDIDATE_LENGTH_M:
                path += _sample_distance(group[end], group[end + 1])
                end += 1
                if path > MAX_CANDIDATE_LENGTH_M:
                    break
            if path < MIN_CANDIDATE_LENGTH_M:
                continue
            fit = _fit_window(group[start : end + 1], group[start].original_index)
            if fit is None:
                continue
            if (
                fit.span_m >= 300.0
                and fit.straightness >= 0.75
                and fit.cross_track_p95_m <= 75.0
            ):
                fits.append(fit)
    return fits


def _cross_track_distance_to_fit(sample: _Sample, fit: _WindowFit) -> float:
    """Return a sample's perpendicular distance from a fitted runway axis."""
    anchor = fit.samples[0]
    east_delta = sample.east_m - anchor.east_m
    north_delta = sample.north_m - anchor.north_m
    heading = math.radians(fit.heading_deg)
    east_axis = math.sin(heading)
    north_axis = math.cos(heading)
    return abs(-east_delta * north_axis + north_delta * east_axis)


def _cluster_candidate_windows(fits: Sequence[_WindowFit]) -> list[_RunwayEvent]:
    """Combine overlapping collinear windows into chronological runway events."""
    clusters: list[list[_WindowFit]] = []
    for fit in sorted(fits, key=lambda item: item.samples[0].original_index):
        fit_start = fit.samples[0].original_index
        matching_cluster = None
        for cluster in reversed(clusters):
            cluster_end = max(item.samples[-1].original_index for item in cluster)
            if fit_start > cluster_end:
                break
            representative = max(cluster, key=lambda item: item.score)
            if (
                abs(_angular_difference(fit.heading_deg, representative.heading_deg))
                <= MAX_EVENT_HEADING_DIFFERENCE_DEG
            ):
                matching_cluster = cluster
                break
        if matching_cluster is None:
            clusters.append([fit])
        else:
            matching_cluster.append(fit)

    events: list[_RunwayEvent] = []
    for cluster in clusters:
        best_fit = max(cluster, key=lambda item: item.score)
        aligned_cluster = [
            fit
            for fit in cluster
            if (
                abs(_angular_difference(fit.heading_deg, best_fit.heading_deg))
                <= MAX_EVENT_HEADING_DIFFERENCE_DEG
                and _cross_track_distance_to_fit(fit.samples[0], best_fit) <= 30.0
            )
        ]
        threshold = min(
            (fit.samples[0] for fit in aligned_cluster),
            key=lambda sample: sample.original_index,
        )
        climbs = [
            fit.climb_m for fit in aligned_cluster if fit.climb_m is not None
        ]
        backtracked_distance, _ = inverse_distance_bearing(
            threshold.point.latitude,
            threshold.point.longitude,
            best_fit.samples[0].point.latitude,
            best_fit.samples[0].point.longitude,
        )
        events.append(
            _RunwayEvent(
                fits=tuple(aligned_cluster),
                best_fit=best_fit,
                threshold=threshold,
                end_index=max(
                    fit.samples[-1].original_index for fit in aligned_cluster
                ),
                maximum_climb_m=max(climbs) if climbs else None,
                minimum_climb_m=min(climbs) if climbs else None,
                backtracked_distance_m=backtracked_distance,
            )
        )
    return events


def _fit_confidence(fit: _WindowFit) -> RunwayConfidence:
    if (
        fit.span_m >= 700.0
        and fit.straightness >= 0.95
        and fit.cross_track_p95_m <= 20.0
        and fit.heading_dispersion_deg <= 8.0
    ):
        return RunwayConfidence.HIGH
    if (
        fit.span_m >= 400.0
        and fit.straightness >= 0.88
        and fit.cross_track_p95_m <= 40.0
        and fit.heading_dispersion_deg <= 18.0
    ):
        return RunwayConfidence.MEDIUM
    return RunwayConfidence.LOW


def _threshold_confidence(
    event: _RunwayEvent,
    point_count: int,
) -> RunwayConfidence:
    """Grade the anchor separately from the more readily observed runway axis."""
    if event.threshold.original_index <= max(10, point_count // 100):
        return RunwayConfidence.HIGH
    if (
        event.backtracked_distance_m >= 100.0
        and _fit_confidence(event.best_fit) is RunwayConfidence.HIGH
    ):
        return RunwayConfidence.MEDIUM
    return RunwayConfidence.LOW


def _select_departure_event(
    events: Sequence[_RunwayEvent],
    samples: Sequence[_Sample],
    altitude_mode: str,
) -> tuple[_RunwayEvent | None, bool]:
    """Prefer a low-altitude climbing runway event over arrivals or fly-bys."""
    if not events:
        return None, False

    altitude_is_usable = altitude_mode not in {"clampToGround", "clampToSeaFloor"}
    altitudes = [
        sample.point.altitude_m
        for sample in samples
        if sample.point.altitude_m is not None
    ]
    considered = list(events)
    if altitude_is_usable and altitudes:
        low_reference = _percentile(altitudes, 0.20)
        low_events = [
            event
            for event in considered
            if event.best_fit.initial_altitude_m is None
            or event.best_fit.initial_altitude_m <= low_reference + 100.0
        ]
        if low_events:
            considered = low_events

        departures = [
            event
            for event in considered
            if event.maximum_climb_m is not None
            and event.maximum_climb_m >= MIN_DEPARTURE_CLIMB_M
        ]
        if departures:
            return min(
                departures,
                key=lambda event: event.threshold.original_index,
            ), True

        non_arrivals = [
            event
            for event in considered
            if event.minimum_climb_m is None
            or event.minimum_climb_m > -MIN_DEPARTURE_CLIMB_M
        ]
        if non_arrivals:
            considered = non_arrivals

    return max(considered, key=lambda event: event.best_fit.score), False


def _theil_sen_slope(samples: Sequence[_GroundElevationSample]) -> float | None:
    """Return a robust altitude slope, ignoring unstable near-zero baselines."""
    slopes = [
        (right.altitude_m - left.altitude_m)
        / (right.along_runway_m - left.along_runway_m)
        for left_index, left in enumerate(samples)
        for right in samples[left_index + 1 :]
        if right.along_runway_m - left.along_runway_m
        >= MIN_SLOPE_PAIR_DISTANCE_M
    ]
    return statistics.median(slopes) if slopes else None


def _ground_elevation_samples(
    event: _RunwayEvent,
) -> list[_GroundElevationSample]:
    """Collect unique absolute-altitude samples near the provisional threshold."""
    best = event.best_fit
    heading = math.radians(best.heading_deg)
    east_axis = math.sin(heading)
    north_axis = math.cos(heading)
    threshold = event.threshold
    unique_samples = {
        sample.original_index: sample
        for fit in event.fits
        for sample in fit.samples
    }
    projected: list[_GroundElevationSample] = []
    for sample in unique_samples.values():
        if sample.point.altitude_m is None:
            continue
        east_delta = sample.east_m - threshold.east_m
        north_delta = sample.north_m - threshold.north_m
        along_runway = east_delta * east_axis + north_delta * north_axis
        cross_track = abs(-east_delta * north_axis + north_delta * east_axis)
        if (
            0.0 <= along_runway <= GROUND_ELEVATION_SEARCH_DISTANCE_M
            and cross_track <= GROUND_ELEVATION_CROSS_TRACK_M
        ):
            projected.append(
                _GroundElevationSample(
                    along_runway_m=along_runway,
                    altitude_m=sample.point.altitude_m,
                    original_index=sample.original_index,
                )
            )
    return sorted(
        projected,
        key=lambda sample: (sample.along_runway_m, sample.original_index),
    )


def _estimate_ground_elevation(
    event: _RunwayEvent,
) -> _GroundElevationEstimate | None:
    """Choose the lowest stable altitude window near the runway threshold."""
    samples = _ground_elevation_samples(event)
    estimates: list[_GroundElevationEstimate] = []
    for start_index in range(len(samples)):
        end_index = start_index + MIN_GROUND_ELEVATION_SAMPLES - 1
        while (
            end_index < len(samples)
            and samples[end_index].along_runway_m
            - samples[start_index].along_runway_m
            < MIN_GROUND_ELEVATION_SPAN_M
        ):
            end_index += 1
        if end_index >= len(samples):
            continue
        window = samples[start_index : end_index + 1]
        span = window[-1].along_runway_m - window[0].along_runway_m
        if span > MAX_GROUND_ELEVATION_SPAN_M:
            continue
        altitudes = [sample.altitude_m for sample in window]
        spread = _percentile(altitudes, 0.90) - _percentile(altitudes, 0.10)
        slope = _theil_sen_slope(window)
        if (
            slope is not None
            and spread <= MAX_GROUND_ELEVATION_SPREAD_M
            and abs(slope) <= MAX_GROUND_ELEVATION_SLOPE
        ):
            estimates.append(
                _GroundElevationEstimate(
                    elevation_m=statistics.median(altitudes),
                    sample_count=len(window),
                    start_distance_m=window[0].along_runway_m,
                    end_distance_m=window[-1].along_runway_m,
                    altitude_spread_m=spread,
                    slope=slope,
                )
            )
    if not estimates:
        return None
    return min(
        estimates,
        key=lambda estimate: (estimate.elevation_m, estimate.start_distance_m),
    )


def infer_departure_runway(
    track: KmlTrack,
    fallback_elevation_m: float | None = None,
) -> RunwayInferenceResult:
    """Infer one reviewable departure threshold and directed true heading."""
    samples, discarded = _local_samples(track)
    if len(samples) < 2:
        return RunwayInferenceResult(
            candidate=None,
            error="The track contains no movement after stationary GPS jitter is removed.",
        )
    groups, discontinuities = _contiguous_groups(samples)
    fits = _candidate_windows(groups)
    events = _cluster_candidate_windows(fits)
    selected_event, climb_confirmed = _select_departure_event(
        events,
        samples,
        track.altitude_mode,
    )
    if selected_event is None:
        return RunwayInferenceResult(
            candidate=None,
            warnings=(
                f"Ignored {discarded} stationary or sub-{JITTER_DISTANCE_M:g} m point(s).",
                f"Detected {discontinuities} position discontinuity/discontinuities.",
            ),
            error=(
                "No sustained straight departure segment could be inferred; "
                "enter the source threshold, true heading, and elevation manually."
            ),
        )

    best = selected_event.best_fit
    heading_confidence = _fit_confidence(best)
    threshold_confidence = _threshold_confidence(
        selected_event,
        len(track.points),
    )

    elevation_estimate = (
        _estimate_ground_elevation(selected_event)
        if track.altitude_mode == "absolute"
        else None
    )
    elevation = (
        elevation_estimate.elevation_m
        if elevation_estimate is not None
        else fallback_elevation_m
        if track.altitude_mode == "absolute"
        else None
    )
    threshold = selected_event.threshold
    warnings: list[str] = []
    if threshold.original_index > max(10, len(track.points) // 100):
        warnings.append(
            "The inferred threshold is not at the beginning of the track; verify that taxiing was excluded."
        )
    if discontinuities:
        warnings.append(
            f"The track contains {discontinuities} position discontinuity/discontinuities."
        )
    if heading_confidence is RunwayConfidence.LOW:
        warnings.append("The runway fit is weak and should be corrected manually if necessary.")
    if threshold_confidence is RunwayConfidence.LOW:
        warnings.append(
            "The runway heading is clearer than the exact threshold; verify the proposed anchor."
        )
    if not climb_confirmed:
        warnings.append(
            "No sustained climb confirmed this as a departure; verify the selected runway event."
        )
    elevation_evidence: tuple[str, ...] = ()
    if track.altitude_mode == "absolute" and elevation_estimate is not None:
        elevation_evidence = (
            (
                "Ground reference elevation: "
                f"{elevation_estimate.elevation_m:.2f} m from "
                f"{elevation_estimate.sample_count} stable sample(s)"
            ),
            (
                "Ground elevation window: "
                f"{elevation_estimate.start_distance_m:.0f}–"
                f"{elevation_estimate.end_distance_m:.0f} m along runway, "
                f"{elevation_estimate.altitude_spread_m:.2f} m spread, "
                f"{elevation_estimate.slope * 100.0:+.2f}% slope"
            ),
        )
    elif track.altitude_mode == "absolute" and fallback_elevation_m is not None:
        elevation_evidence = (
            f"Ground reference elevation: {fallback_elevation_m:.2f} m preset fallback",
        )
        warnings.append(
            "No stable runway-aligned ground elevation window was found; verify the preset fallback."
        )
    elif track.altitude_mode == "absolute":
        elevation_evidence = (
            "Ground reference elevation: no stable runway-aligned window found",
        )
        warnings.append(
            "Source ground-reference elevation could not be inferred; enter it manually."
        )

    evidence = (
        f"Aligned distance: {best.span_m:.0f} m",
        f"95% cross-track error: {best.cross_track_p95_m:.1f} m",
        f"Median heading dispersion: {best.heading_dispersion_deg:.1f}°",
        f"Ignored stationary jitter points: {discarded}",
        (
            f"Altitude change through candidate: {best.climb_m:+.1f} m"
            if best.climb_m is not None
            else "Altitude trend unavailable"
        ),
    ) + elevation_evidence + (
        f"Backtracked aligned ground distance: {selected_event.backtracked_distance_m:.0f} m",
        (
            "Departure classification: climb confirmed"
            if climb_confirmed
            else "Departure classification: not confirmed by climb"
        ),
    )
    return RunwayInferenceResult(
        candidate=RunwayCandidate(
            reference=RunwayReference(
                latitude=threshold.point.latitude,
                longitude=threshold.point.longitude,
                true_heading_deg=best.heading_deg,
                elevation_m=elevation,
            ),
            heading_confidence=heading_confidence,
            threshold_confidence=threshold_confidence,
            start_index=threshold.original_index,
            end_index=selected_event.end_index,
            aligned_distance_m=best.span_m,
            heading_dispersion_deg=best.heading_dispersion_deg,
            cross_track_error_m=best.cross_track_p95_m,
            evidence=evidence,
            warnings=tuple(warnings),
        ),
        warnings=tuple(warnings),
    )
