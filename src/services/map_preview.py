"""Canonical geometry preparation for the Google Maps 3D preview.

The preview deliberately consumes the same quantized :class:`KmlDocument`
that is handed to the KML exporter.  Geographic adjustments are performed in
Python, in a WGS84 local ENU frame, so the browser is only responsible for
rendering coordinates rather than reproducing application geometry logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import math
import re
from typing import Any

from .geodesy import EnuCoordinate, LocalEnuFrame, inverse_distance_bearing
from .kml_export import (
    KmlCoordinate,
    KmlDocument,
    KmlLineString,
    KmlPlacemark,
    KmlPolygon,
    KmlStyle,
    render_kml,
)


MAX_HORIZONTAL_OFFSET_M = 100_000.0
MAX_VERTICAL_OFFSET_M = 20_000.0
MAX_ABSOLUTE_YAW_DEG = 180.0

_KML_COLOUR_RE = re.compile(r"[0-9a-fA-F]{8}\Z")
_MAPS_ALTITUDE_MODES = {
    "absolute": "ABSOLUTE",
    "relativeToGround": "RELATIVE_TO_GROUND",
    "clampToGround": "CLAMP_TO_GROUND",
}


def _finite_float(value: object, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be a finite number.") from error
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number.")
    return number


def _bounded_float(value: object, label: str, maximum_absolute: float) -> float:
    number = _finite_float(value, label)
    if abs(number) > maximum_absolute:
        raise ValueError(
            f"{label} must be between {-maximum_absolute:g} and "
            f"{maximum_absolute:g}."
        )
    return 0.0 if number == 0.0 else number


@dataclass(frozen=True, slots=True)
class TraceAdjustment:
    """A trace transform about its fixed anchor in local ENU coordinates."""

    east_m: float = 0.0
    north_m: float = 0.0
    up_m: float = 0.0
    yaw_deg: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "east_m",
            _bounded_float(
                self.east_m,
                "East offset",
                MAX_HORIZONTAL_OFFSET_M,
            ),
        )
        object.__setattr__(
            self,
            "north_m",
            _bounded_float(
                self.north_m,
                "North offset",
                MAX_HORIZONTAL_OFFSET_M,
            ),
        )
        object.__setattr__(
            self,
            "up_m",
            _bounded_float(
                self.up_m,
                "Up offset",
                MAX_VERTICAL_OFFSET_M,
            ),
        )
        object.__setattr__(
            self,
            "yaw_deg",
            _bounded_float(
                self.yaw_deg,
                "Yaw",
                MAX_ABSOLUTE_YAW_DEG,
            ),
        )

    @property
    def is_zero(self) -> bool:
        return not any((self.east_m, self.north_m, self.up_m, self.yaw_deg))


def _validated_coordinate(coordinate: KmlCoordinate, label: str) -> KmlCoordinate:
    longitude = _finite_float(coordinate.longitude, f"{label} longitude")
    latitude = _finite_float(coordinate.latitude, f"{label} latitude")
    altitude = _finite_float(coordinate.altitude_m, f"{label} altitude")
    if not -180.0 <= longitude <= 180.0:
        raise ValueError(f"{label} longitude must be between -180 and 180 degrees.")
    if not -90.0 <= latitude <= 90.0:
        raise ValueError(f"{label} latitude must be between -90 and 90 degrees.")
    return KmlCoordinate(longitude, latitude, altitude)


def _maps_altitude_mode(altitude_mode: str) -> str:
    try:
        return _MAPS_ALTITUDE_MODES[altitude_mode]
    except KeyError as error:
        raise ValueError(f'Unsupported KML altitude mode "{altitude_mode}".') from error


def _transform_coordinate(
    coordinate: KmlCoordinate,
    frame: LocalEnuFrame,
    adjustment: TraceAdjustment,
    *,
    cosine: float,
    sine: float,
    altitude_mode: str,
) -> KmlCoordinate:
    coordinate = _validated_coordinate(coordinate, "Trace coordinate")
    source = frame.to_enu(coordinate.latitude, coordinate.longitude)
    adjusted = EnuCoordinate(
        east_m=(source.east_m * cosine + source.north_m * sine) + adjustment.east_m,
        north_m=(-source.east_m * sine + source.north_m * cosine)
        + adjustment.north_m,
        # KML altitude is independent of the neutral ellipsoidal height used
        # by LocalEnuFrame.  Preserve the point's curvature term here.
        up_m=source.up_m,
    )
    latitude, longitude = frame.to_wgs84(adjusted)
    altitude = (
        adjustment.up_m
        if altitude_mode == "clampToGround" and adjustment.up_m != 0.0
        else coordinate.altitude_m + adjustment.up_m
    )
    return KmlCoordinate(longitude, latitude, altitude)


def apply_enu_adjustment(
    document: KmlDocument,
    anchor: KmlCoordinate,
    adjustment: TraceAdjustment,
) -> KmlDocument:
    """Apply one non-cumulative ENU adjustment to every geometry in a document.

    A zero adjustment intentionally returns ``document`` itself.  This keeps
    the established zero-offset export path byte-for-byte compatible until the
    explicit preview quantization step is requested.
    """

    if not isinstance(document, KmlDocument):
        raise TypeError("document must be a KmlDocument.")
    if not isinstance(adjustment, TraceAdjustment):
        raise TypeError("adjustment must be a TraceAdjustment.")
    validated_anchor = _validated_coordinate(anchor, "Anchor")
    if adjustment.is_zero:
        return document

    frame = LocalEnuFrame(validated_anchor.latitude, validated_anchor.longitude)
    yaw = math.radians(adjustment.yaw_deg)
    cosine = math.cos(yaw)
    sine = math.sin(yaw)
    placemarks: list[KmlPlacemark] = []

    for placemark in document.placemarks:
        geometry = placemark.geometry
        if isinstance(geometry, KmlLineString):
            altitude_mode = geometry.altitude_mode
            coordinates = tuple(
                _transform_coordinate(
                    coordinate,
                    frame,
                    adjustment,
                    cosine=cosine,
                    sine=sine,
                    altitude_mode=altitude_mode,
                )
                for coordinate in geometry.coordinates
            )
            transformed_geometry = replace(
                geometry,
                coordinates=coordinates,
                altitude_mode=(
                    "relativeToGround"
                    if altitude_mode == "clampToGround" and adjustment.up_m != 0.0
                    else altitude_mode
                ),
            )
        elif isinstance(geometry, KmlPolygon):
            altitude_mode = geometry.altitude_mode
            outer_ring = tuple(
                _transform_coordinate(
                    coordinate,
                    frame,
                    adjustment,
                    cosine=cosine,
                    sine=sine,
                    altitude_mode=altitude_mode,
                )
                for coordinate in geometry.outer_ring
            )
            transformed_geometry = replace(
                geometry,
                outer_ring=outer_ring,
                altitude_mode=(
                    "relativeToGround"
                    if altitude_mode == "clampToGround" and adjustment.up_m != 0.0
                    else altitude_mode
                ),
            )
        else:  # Defensive in case KmlGeometry is extended without this service.
            raise TypeError("Unsupported KML geometry in preview document.")
        placemarks.append(replace(placemark, geometry=transformed_geometry))

    return replace(document, placemarks=tuple(placemarks))


def _quantized_value(value: object, places: int, label: str) -> float:
    rounded = round(_finite_float(value, label), places)
    return 0.0 if rounded == 0.0 else rounded


def _quantized_coordinate(coordinate: KmlCoordinate) -> KmlCoordinate:
    coordinate = _validated_coordinate(coordinate, "KML coordinate")
    return KmlCoordinate(
        longitude=_quantized_value(coordinate.longitude, 7, "KML longitude"),
        latitude=_quantized_value(coordinate.latitude, 7, "KML latitude"),
        altitude_m=_quantized_value(coordinate.altitude_m, 3, "KML altitude"),
    )


def quantize_kml_document(document: KmlDocument) -> KmlDocument:
    """Return the exact coordinate document shared by preview and KML output.

    Polygon rings are explicitly closed here because the KML renderer closes
    them during serialization.  Making that closure part of the canonical
    document ensures the Maps payload has the same topology.
    """

    if not isinstance(document, KmlDocument):
        raise TypeError("document must be a KmlDocument.")
    placemarks: list[KmlPlacemark] = []
    for placemark in document.placemarks:
        geometry = placemark.geometry
        if isinstance(geometry, KmlLineString):
            transformed_geometry = replace(
                geometry,
                coordinates=tuple(
                    _quantized_coordinate(coordinate)
                    for coordinate in geometry.coordinates
                ),
            )
        elif isinstance(geometry, KmlPolygon):
            outer_ring = tuple(
                _quantized_coordinate(coordinate)
                for coordinate in geometry.outer_ring
            )
            if outer_ring and outer_ring[0] != outer_ring[-1]:
                outer_ring = (*outer_ring, outer_ring[0])
            transformed_geometry = replace(geometry, outer_ring=outer_ring)
        else:
            raise TypeError("Unsupported KML geometry in preview document.")
        placemarks.append(replace(placemark, geometry=transformed_geometry))

    canonical = replace(document, placemarks=tuple(placemarks))
    # Reuse the exporter's public validation so payload creation cannot accept
    # a scene that the corresponding KML export would reject.
    render_kml(canonical)
    return canonical


@dataclass(frozen=True, slots=True)
class PreparedTrace:
    """One base document and its current canonical adjusted representation."""

    trace_id: str
    label: str
    anchor: KmlCoordinate
    base_document: KmlDocument
    adjustment: TraceAdjustment = field(default_factory=TraceAdjustment)
    anchor_altitude_mode: str = "clampToGround"
    adjusted_document: KmlDocument = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.trace_id, str) or not self.trace_id.strip():
            raise ValueError("Trace ID must not be empty.")
        if not isinstance(self.label, str) or not self.label.strip():
            raise ValueError("Trace label must not be empty.")
        if not isinstance(self.adjustment, TraceAdjustment):
            raise TypeError("adjustment must be a TraceAdjustment.")
        _maps_altitude_mode(self.anchor_altitude_mode)
        validated_anchor = _validated_coordinate(self.anchor, "Anchor")
        object.__setattr__(self, "anchor", validated_anchor)
        object.__setattr__(
            self,
            "adjusted_document",
            quantize_kml_document(
                apply_enu_adjustment(
                    self.base_document,
                    validated_anchor,
                    self.adjustment,
                )
            ),
        )

    def with_adjustment(self, adjustment: TraceAdjustment) -> PreparedTrace:
        """Create a fresh trace from the original base, never the prior output."""

        return replace(self, adjustment=adjustment)

    @property
    def adjusted_anchor(self) -> KmlCoordinate:
        """Return the trace anchor after its current ENU translation."""

        frame = LocalEnuFrame(self.anchor.latitude, self.anchor.longitude)
        latitude, longitude = frame.to_wgs84(
            EnuCoordinate(
                east_m=self.adjustment.east_m,
                north_m=self.adjustment.north_m,
                up_m=0.0,
            )
        )
        altitude = (
            self.adjustment.up_m
            if self.anchor_altitude_mode == "clampToGround"
            and self.adjustment.up_m != 0.0
            else self.anchor.altitude_m + self.adjustment.up_m
        )
        return KmlCoordinate(longitude, latitude, altitude)

    @property
    def adjusted_anchor_altitude_mode(self) -> str:
        if (
            self.anchor_altitude_mode == "clampToGround"
            and self.adjustment.up_m != 0.0
        ):
            return "relativeToGround"
        return self.anchor_altitude_mode

    def with_anchor_destination(
        self,
        latitude: float,
        longitude: float,
    ) -> PreparedTrace:
        """Move the anchor to one WGS84 destination without cumulative drift."""

        destination = _validated_coordinate(
            KmlCoordinate(longitude, latitude, self.anchor.altitude_m),
            "Anchor destination",
        )
        surface_distance_m, _ = inverse_distance_bearing(
            self.anchor.latitude,
            self.anchor.longitude,
            destination.latitude,
            destination.longitude,
        )
        if surface_distance_m > math.sqrt(2.0) * MAX_HORIZONTAL_OFFSET_M:
            raise ValueError(
                "Anchor destination is outside the supported "
                "±100000 m East/North adjustment bounds."
            )
        frame = LocalEnuFrame(self.anchor.latitude, self.anchor.longitude)
        position = frame.to_enu(destination.latitude, destination.longitude)
        return self.with_adjustment(
            replace(
                self.adjustment,
                east_m=position.east_m,
                north_m=position.north_m,
            )
        )


@dataclass(frozen=True, slots=True)
class PreviewScene:
    """A non-empty set of independently adjustable traces shown together."""

    traces: tuple[PreparedTrace, ...]

    def __post_init__(self) -> None:
        traces = tuple(self.traces)
        if not traces:
            raise ValueError("A preview scene requires at least one trace.")
        if not all(isinstance(trace, PreparedTrace) for trace in traces):
            raise TypeError("Preview scene entries must be PreparedTrace values.")
        trace_ids = [trace.trace_id for trace in traces]
        if len(set(trace_ids)) != len(trace_ids):
            raise ValueError("Preview trace IDs must be unique.")
        object.__setattr__(self, "traces", traces)


def kml_colour_to_css(colour: str) -> str:
    """Convert an eight-digit KML ``aabbggrr`` colour to CSS ``#rrggbbaa``."""

    if not isinstance(colour, str) or _KML_COLOUR_RE.fullmatch(colour) is None:
        raise ValueError("KML colour must be an eight-digit aabbggrr value.")
    alpha, blue, green, red = (
        colour[0:2],
        colour[2:4],
        colour[4:6],
        colour[6:8],
    )
    return f"#{red}{green}{blue}{alpha}".lower()


def _coordinate_payload(coordinate: KmlCoordinate) -> dict[str, float]:
    return {
        "lat": coordinate.latitude,
        "lng": coordinate.longitude,
        "altitude": coordinate.altitude_m,
    }


def _style_payload(style: KmlStyle) -> dict[str, object]:
    return {
        "strokeColor": kml_colour_to_css(style.line_colour),
        # KML export writes line widths at three decimal places.  Mirror that
        # serialization boundary in Maps rather than leaking extra precision.
        "strokeWidth": _quantized_value(
            style.line_width,
            3,
            "KML line width",
        ),
        "fillColor": (
            kml_colour_to_css(style.poly_colour)
            if style.poly_colour is not None
            else None
        ),
    }


def _geometry_payload(
    placemark: KmlPlacemark,
    style: KmlStyle,
    geometry_id: str,
) -> dict[str, object]:
    geometry = placemark.geometry
    common: dict[str, object] = {
        "id": geometry_id,
        "name": placemark.name,
        "description": placemark.description,
        "style": _style_payload(style),
        "altitudeMode": _maps_altitude_mode(geometry.altitude_mode),
    }
    if isinstance(geometry, KmlLineString):
        return {
            **common,
            "type": "polyline",
            "extrude": geometry.extrude_to_ground,
            "tessellate": geometry.tessellate,
            "coordinates": [
                _coordinate_payload(coordinate)
                for coordinate in geometry.coordinates
            ],
        }
    if isinstance(geometry, KmlPolygon):
        return {
            **common,
            "type": "polygon",
            "coordinates": [
                _coordinate_payload(coordinate)
                for coordinate in geometry.outer_ring
            ],
        }
    raise TypeError("Unsupported KML geometry in preview document.")


def preview_payload(scene: PreviewScene) -> dict[str, Any]:
    """Build a JSON-serializable Maps payload from canonical trace documents."""

    if not isinstance(scene, PreviewScene):
        raise TypeError("scene must be a PreviewScene.")
    traces: list[dict[str, object]] = []
    for trace in scene.traces:
        document = trace.adjusted_document
        styles: dict[str, KmlStyle] = {}
        for style in document.styles:
            if style.style_id in styles:
                raise ValueError(f'Duplicate KML style ID "{style.style_id}".')
            styles[style.style_id] = style
        geometries: list[dict[str, object]] = []
        for geometry_index, placemark in enumerate(document.placemarks):
            if not placemark.style_url.startswith("#"):
                raise ValueError(
                    f'Placemark "{placemark.name}" has an invalid style URL.'
                )
            try:
                style = styles[placemark.style_url[1:]]
            except KeyError as error:
                raise ValueError(
                    f'Placemark "{placemark.name}" references an unknown KML style.'
                ) from error
            geometries.append(
                _geometry_payload(
                    placemark,
                    style,
                    f"geometry-{geometry_index}",
                )
            )

        anchor = _quantized_coordinate(trace.adjusted_anchor)
        traces.append(
            {
                "id": trace.trace_id,
                "label": trace.label,
                "adjustment": {
                    "eastM": trace.adjustment.east_m,
                    "northM": trace.adjustment.north_m,
                    "upM": trace.adjustment.up_m,
                    "yawDeg": trace.adjustment.yaw_deg,
                },
                "anchor": {
                    **_coordinate_payload(anchor),
                    "altitudeMode": _maps_altitude_mode(
                        trace.adjusted_anchor_altitude_mode
                    ),
                    "label": "Trace anchor",
                    "color": "#ff00ffff",
                },
                "geometries": geometries,
            }
        )
    return {"version": 2, "traces": traces}


__all__ = [
    "MAX_ABSOLUTE_YAW_DEG",
    "MAX_HORIZONTAL_OFFSET_M",
    "MAX_VERTICAL_OFFSET_M",
    "PreparedTrace",
    "PreviewScene",
    "TraceAdjustment",
    "apply_enu_adjustment",
    "kml_colour_to_css",
    "preview_payload",
    "quantize_kml_document",
]
