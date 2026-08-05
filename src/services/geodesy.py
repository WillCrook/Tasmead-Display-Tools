"""WGS84 geodesic and local ENU operations used by application services."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
import math

from pyproj import Geod, Transformer
from pyproj.enums import TransformDirection
from pyproj.exceptions import GeodError, ProjError


_WGS84_GEOD = Geod(ellps="WGS84")
_NEUTRAL_ELLIPSOIDAL_HEIGHT_M = 0.0


def _finite(value: object, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be a finite number.") from error
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number.")
    return number


def _coordinate(latitude: object, longitude: object) -> tuple[float, float]:
    latitude_value = _finite(latitude, "Latitude")
    longitude_value = _finite(longitude, "Longitude")
    if not -90.0 <= latitude_value <= 90.0:
        raise ValueError("Latitude must be between -90 and 90 degrees.")
    if not -180.0 <= longitude_value <= 180.0:
        raise ValueError("Longitude must be between -180 and 180 degrees.")
    return latitude_value, longitude_value


def _normalized_longitude(longitude: float) -> float:
    return (longitude + 180.0) % 360.0 - 180.0


def inverse_distance_bearing(
    start_latitude: float,
    start_longitude: float,
    end_latitude: float,
    end_longitude: float,
) -> tuple[float, float]:
    """Return WGS84 ellipsoidal distance and initial true bearing."""
    start_latitude, start_longitude = _coordinate(
        start_latitude,
        start_longitude,
    )
    end_latitude, end_longitude = _coordinate(end_latitude, end_longitude)
    if (start_latitude, start_longitude) == (end_latitude, end_longitude):
        return 0.0, 0.0
    try:
        initial_bearing, _, distance = _WGS84_GEOD.inv(
            start_longitude,
            start_latitude,
            end_longitude,
            end_latitude,
        )
    except GeodError as error:
        raise ValueError("WGS84 inverse transformation failed.") from error
    if not all(math.isfinite(value) for value in (initial_bearing, distance)):
        raise ValueError("WGS84 inverse transformation returned a non-finite result.")
    if distance == 0.0:
        return 0.0, 0.0
    return distance, initial_bearing % 360.0


def destination_point(
    latitude: float,
    longitude: float,
    distance_m: float,
    true_bearing_deg: float,
) -> tuple[float, float]:
    """Project a point along a WGS84 ellipsoidal geodesic."""
    latitude, longitude = _coordinate(latitude, longitude)
    distance = _finite(distance_m, "Distance")
    bearing = _finite(true_bearing_deg, "True bearing")
    try:
        final_longitude, final_latitude, _ = _WGS84_GEOD.fwd(
            longitude,
            latitude,
            bearing,
            distance,
        )
    except GeodError as error:
        raise ValueError("WGS84 direct transformation failed.") from error
    if not all(math.isfinite(value) for value in (final_latitude, final_longitude)):
        raise ValueError("WGS84 direct transformation returned a non-finite result.")
    return final_latitude, _normalized_longitude(final_longitude)


@dataclass(frozen=True, slots=True)
class EnuCoordinate:
    """A position in a local east, north, up frame, expressed in metres."""

    east_m: float
    north_m: float
    up_m: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "east_m", _finite(self.east_m, "ENU east"))
        object.__setattr__(self, "north_m", _finite(self.north_m, "ENU north"))
        object.__setattr__(self, "up_m", _finite(self.up_m, "ENU up"))


@dataclass(frozen=True, slots=True)
class LocalEnuFrame:
    """A WGS84 topocentric frame anchored at a geodetic coordinate.

    KML altitude is deliberately not supplied to this frame. A neutral
    ellipsoidal height isolates horizontal positioning from KML's independent
    altitude modes and ground-reference semantics.
    """

    latitude: float
    longitude: float
    _transformer: Transformer = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        latitude, longitude = _coordinate(self.latitude, self.longitude)
        pipeline = (
            "+proj=pipeline "
            "+step +proj=cart +ellps=WGS84 "
            "+step +proj=topocentric +ellps=WGS84 "
            f"+lat_0={latitude:.17g} +lon_0={longitude:.17g} "
            f"+h_0={_NEUTRAL_ELLIPSOIDAL_HEIGHT_M:.1f}"
        )
        try:
            transformer = Transformer.from_pipeline(pipeline)
        except ProjError as error:
            raise ValueError("WGS84 ENU frame construction failed.") from error
        object.__setattr__(self, "latitude", latitude)
        object.__setattr__(self, "longitude", longitude)
        object.__setattr__(self, "_transformer", transformer)

    def to_enu(self, latitude: float, longitude: float) -> EnuCoordinate:
        """Convert WGS84 latitude/longitude to this frame at neutral height."""
        latitude, longitude = _coordinate(latitude, longitude)
        try:
            east, north, up = self._transformer.transform(
                longitude,
                latitude,
                _NEUTRAL_ELLIPSOIDAL_HEIGHT_M,
                errcheck=True,
            )
        except ProjError as error:
            raise ValueError("WGS84 to ENU transformation failed.") from error
        return EnuCoordinate(east, north, up)

    def to_wgs84(self, position: EnuCoordinate) -> tuple[float, float]:
        """Convert an ENU position back to WGS84 latitude/longitude."""
        try:
            longitude, latitude, height = self._transformer.transform(
                position.east_m,
                position.north_m,
                position.up_m,
                direction=TransformDirection.INVERSE,
                errcheck=True,
            )
        except ProjError as error:
            raise ValueError("ENU to WGS84 transformation failed.") from error
        if not all(math.isfinite(value) for value in (latitude, longitude, height)):
            raise ValueError("ENU to WGS84 transformation returned a non-finite result.")
        latitude, longitude = _coordinate(latitude, _normalized_longitude(longitude))
        return latitude, longitude


def transpose_wgs84_enu_points(
    points: Iterable[tuple[float, float, float]],
    source_origin: tuple[float, float],
    target_origin: tuple[float, float],
    heading_delta_deg: float,
) -> tuple[tuple[float, float, float], ...]:
    """Move and rotate WGS84 points between two local runway ENU frames."""
    source_frame = LocalEnuFrame(*source_origin)
    target_frame = LocalEnuFrame(*target_origin)
    heading_delta = math.radians(_finite(heading_delta_deg, "Heading delta"))
    cosine = math.cos(heading_delta)
    sine = math.sin(heading_delta)

    transformed: list[tuple[float, float, float]] = []
    for latitude, longitude, altitude in points:
        preserved_altitude = _finite(altitude, "Route altitude")
        source_position = source_frame.to_enu(latitude, longitude)
        rotated_position = EnuCoordinate(
            east_m=(
                source_position.east_m * cosine
                + source_position.north_m * sine
            ),
            north_m=(
                -source_position.east_m * sine
                + source_position.north_m * cosine
            ),
            up_m=source_position.up_m,
        )
        final_latitude, final_longitude = target_frame.to_wgs84(rotated_position)
        transformed.append(
            (final_latitude, final_longitude, preserved_altitude)
        )
    return tuple(transformed)


__all__ = [
    "EnuCoordinate",
    "LocalEnuFrame",
    "destination_point",
    "inverse_distance_bearing",
    "transpose_wgs84_enu_points",
]
