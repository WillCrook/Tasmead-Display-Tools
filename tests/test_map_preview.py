import math
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from services.geodesy import (
    EnuCoordinate,
    LocalEnuFrame,
    destination_point,
    inverse_distance_bearing,
)
from services.kml_export import (
    KML_NAMESPACE,
    KmlCoordinate,
    KmlDocument,
    KmlLineString,
    KmlPlacemark,
    KmlPolygon,
    KmlStyle,
    render_kml,
)
from services.map_preview import (
    PreparedTrace,
    PreviewScene,
    TraceAdjustment,
    apply_enu_adjustment,
    kml_colour_to_css,
    preview_payload,
    quantize_kml_document,
)


NAMESPACE = {"kml": KML_NAMESPACE}
ANCHOR = KmlCoordinate(longitude=-1.0, latitude=51.0, altitude_m=80.0)


def _point(frame, east_m, north_m, altitude_m):
    latitude, longitude = frame.to_wgs84(
        EnuCoordinate(east_m=east_m, north_m=north_m, up_m=0.0)
    )
    return KmlCoordinate(longitude, latitude, altitude_m)


def _document(*, open_polygon=False):
    frame = LocalEnuFrame(ANCHOR.latitude, ANCHOR.longitude)
    path = KmlPlacemark(
        name="Flight path",
        description="Canonical path",
        style_url="#path",
        geometry=KmlLineString(
            coordinates=(
                _point(frame, 0.0, 0.0, 100.12356),
                _point(frame, 10.0, 20.0, 125.98764),
            ),
            altitude_mode="absolute",
            extrude_to_ground=True,
        ),
    )
    ring = (
        _point(frame, -10.0, -10.0, 9.0),
        _point(frame, 10.0, -10.0, 9.0),
        _point(frame, 10.0, 10.0, 9.0),
    )
    if not open_polygon:
        ring = (*ring, ring[0])
    zone = KmlPlacemark(
        name="Ground zone",
        style_url="#zone",
        geometry=KmlPolygon(
            outer_ring=ring,
            altitude_mode="clampToGround",
        ),
    )
    return KmlDocument(
        name="Preview",
        styles=(
            KmlStyle("path", "aaff00ff", 6.0),
            KmlStyle("zone", "ff332211", 2.5006, "80403020"),
        ),
        placemarks=(path, zone),
    )


class TraceAdjustmentTests(unittest.TestCase):
    def test_defaults_are_zero_and_negative_zero_is_normalized(self):
        self.assertTrue(TraceAdjustment().is_zero)
        self.assertEqual(TraceAdjustment(east_m=-0.0).east_m, 0.0)
        self.assertEqual(
            math.copysign(1.0, TraceAdjustment(east_m=-0.0).east_m),
            1.0,
        )

    def test_bounds_are_inclusive(self):
        adjustment = TraceAdjustment(
            east_m=100_000,
            north_m=-100_000,
            up_m=20_000,
            yaw_deg=-180,
        )
        self.assertEqual(adjustment.east_m, 100_000.0)
        self.assertEqual(adjustment.yaw_deg, -180.0)

    def test_non_finite_and_out_of_range_values_are_rejected(self):
        cases = (
            (lambda: TraceAdjustment(east_m=float("nan")), "East offset"),
            (lambda: TraceAdjustment(north_m=float("inf")), "North offset"),
            (lambda: TraceAdjustment(up_m=20_000.1), "Up offset"),
            (lambda: TraceAdjustment(yaw_deg=180.1), "Yaw"),
            (lambda: TraceAdjustment(east_m=-100_000.1), "East offset"),
        )
        for operation, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                ValueError, message
            ):
                operation()


class EnuAdjustmentTests(unittest.TestCase):
    def test_zero_adjustment_preserves_original_document_identity(self):
        document = _document()

        adjusted = apply_enu_adjustment(document, ANCHOR, TraceAdjustment())

        self.assertIs(adjusted, document)

    def test_clockwise_yaw_precedes_translation_about_fixed_anchor(self):
        frame = LocalEnuFrame(ANCHOR.latitude, ANCHOR.longitude)
        document = _document()

        adjusted = apply_enu_adjustment(
            document,
            ANCHOR,
            TraceAdjustment(east_m=100.0, north_m=200.0, yaw_deg=90.0),
        )

        line = adjusted.placemarks[0].geometry
        self.assertIsInstance(line, KmlLineString)
        anchor_result = frame.to_enu(
            line.coordinates[0].latitude,
            line.coordinates[0].longitude,
        )
        point_result = frame.to_enu(
            line.coordinates[1].latitude,
            line.coordinates[1].longitude,
        )
        self.assertAlmostEqual(anchor_result.east_m, 100.0, places=5)
        self.assertAlmostEqual(anchor_result.north_m, 200.0, places=5)
        # Original (E=10, N=20), clockwise 90 -> (E=20, N=-10),
        # then translation -> (E=120, N=190).
        self.assertAlmostEqual(point_result.east_m, 120.0, places=5)
        self.assertAlmostEqual(point_result.north_m, 190.0, places=5)
        self.assertEqual(line.coordinates[1].altitude_m, 125.98764)

    def test_up_offset_uses_kml_altitude_domain_and_lifts_clamped_scene(self):
        document = _document()
        original_line = document.placemarks[0].geometry
        relative = KmlPlacemark(
            name="Relative path",
            style_url="#path",
            geometry=KmlLineString(
                coordinates=tuple(
                    KmlCoordinate(
                        point.longitude,
                        point.latitude,
                        20.0 + index,
                    )
                    for index, point in enumerate(original_line.coordinates)
                ),
                altitude_mode="relativeToGround",
            ),
        )
        document = KmlDocument(
            document.name,
            document.styles,
            (*document.placemarks, relative),
        )

        adjusted = apply_enu_adjustment(
            document,
            ANCHOR,
            TraceAdjustment(up_m=-15.5),
        )

        line = adjusted.placemarks[0].geometry
        polygon = adjusted.placemarks[1].geometry
        relative_line = adjusted.placemarks[2].geometry
        self.assertEqual(line.altitude_mode, "absolute")
        self.assertAlmostEqual(line.coordinates[0].altitude_m, 84.62356)
        self.assertEqual(relative_line.altitude_mode, "relativeToGround")
        self.assertEqual(
            tuple(point.altitude_m for point in relative_line.coordinates),
            (4.5, 5.5),
        )
        self.assertEqual(polygon.altitude_mode, "relativeToGround")
        self.assertTrue(
            all(coordinate.altitude_m == -15.5 for coordinate in polygon.outer_ring)
        )
        self.assertEqual(polygon.outer_ring[0], polygon.outer_ring[-1])

    def test_horizontal_only_adjustment_preserves_clamp_mode_and_stored_altitude(self):
        document = _document()

        adjusted = apply_enu_adjustment(
            document,
            ANCHOR,
            TraceAdjustment(east_m=1.0),
        )

        polygon = adjusted.placemarks[1].geometry
        original = document.placemarks[1].geometry
        self.assertEqual(polygon.altitude_mode, "clampToGround")
        self.assertEqual(
            tuple(point.altitude_m for point in polygon.outer_ring),
            tuple(point.altitude_m for point in original.outer_ring),
        )

    def test_prepared_trace_recomputes_from_base_instead_of_prior_adjustment(self):
        base = _document()
        trace = PreparedTrace("route", "Route", ANCHOR, base)
        first = trace.with_adjustment(TraceAdjustment(east_m=500.0))

        second = first.with_adjustment(TraceAdjustment(north_m=250.0))
        direct = PreparedTrace(
            "route",
            "Route",
            ANCHOR,
            base,
            TraceAdjustment(north_m=250.0),
        )

        self.assertEqual(second.adjusted_document, direct.adjusted_document)
        self.assertEqual(second.adjustment.east_m, 0.0)

    def test_anchor_destination_preserves_up_and_yaw_and_is_non_cumulative(self):
        trace = PreparedTrace(
            "route",
            "Route",
            ANCHOR,
            _document(),
            TraceAdjustment(east_m=20.0, north_m=30.0, up_m=45.0, yaw_deg=12.0),
        )
        first_destination = destination_point(
            ANCHOR.latitude,
            ANCHOR.longitude,
            750.0,
            105.0,
        )
        second_destination = destination_point(
            ANCHOR.latitude,
            ANCHOR.longitude,
            1_250.0,
            315.0,
        )

        first = trace.with_anchor_destination(*first_destination)
        second = first.with_anchor_destination(*second_destination)
        direct = trace.with_anchor_destination(*second_destination)

        self.assertEqual(first.adjustment.up_m, 45.0)
        self.assertEqual(first.adjustment.yaw_deg, 12.0)
        self.assertEqual(second.adjustment, direct.adjustment)
        self.assertEqual(second.adjusted_document, direct.adjusted_document)
        self.assertAlmostEqual(
            first.adjusted_anchor.latitude,
            first_destination[0],
            places=6,
        )
        self.assertAlmostEqual(
            first.adjusted_anchor.longitude,
            first_destination[1],
            places=6,
        )
        line = first.adjusted_document.placemarks[0].geometry
        first_point = LocalEnuFrame(
            ANCHOR.latitude,
            ANCHOR.longitude,
        ).to_enu(line.coordinates[0].latitude, line.coordinates[0].longitude)
        second_point = LocalEnuFrame(
            ANCHOR.latitude,
            ANCHOR.longitude,
        ).to_enu(line.coordinates[1].latitude, line.coordinates[1].longitude)
        yaw = math.radians(first.adjustment.yaw_deg)
        expected_east_delta = 10.0 * math.cos(yaw) + 20.0 * math.sin(yaw)
        expected_north_delta = -10.0 * math.sin(yaw) + 20.0 * math.cos(yaw)
        self.assertAlmostEqual(
            second_point.east_m - first_point.east_m,
            expected_east_delta,
            places=2,
        )
        self.assertAlmostEqual(
            second_point.north_m - first_point.north_m,
            expected_north_delta,
            places=2,
        )

    def test_anchor_destination_enforces_existing_horizontal_bounds(self):
        trace = PreparedTrace("route", "Route", ANCHOR, _document())
        destination = destination_point(
            ANCHOR.latitude,
            ANCHOR.longitude,
            150_000.0,
            90.0,
        )

        with self.assertRaisesRegex(ValueError, "adjustment bounds"):
            trace.with_anchor_destination(*destination)

        with self.assertRaisesRegex(ValueError, "adjustment bounds"):
            trace.with_anchor_destination(-ANCHOR.latitude, 179.0)

        self.assertTrue(trace.adjustment.is_zero)

    def test_anchor_destination_handles_high_latitude_antimeridian_crossing(self):
        anchor = KmlCoordinate(179.99, 82.0, 0.0)
        frame = LocalEnuFrame(anchor.latitude, anchor.longitude)
        start = _point(frame, 0.0, 0.0, 10.0)
        document = KmlDocument(
            name="Polar",
            styles=(KmlStyle("path", "ffffffff", 1.0),),
            placemarks=(
                KmlPlacemark(
                    "Path",
                    "#path",
                    KmlLineString((start, start), "absolute"),
                ),
            ),
        )
        trace = PreparedTrace("polar", "Polar", anchor, document)
        destination = destination_point(anchor.latitude, anchor.longitude, 25_000.0, 90.0)

        moved = trace.with_anchor_destination(*destination)

        self.assertLess(destination[1], 0.0)
        anchor_error_m, _ = inverse_distance_bearing(
            moved.adjusted_anchor.latitude,
            moved.adjusted_anchor.longitude,
            *destination,
        )
        self.assertLess(anchor_error_m, 1.0)
        self.assertLess(abs(moved.adjustment.east_m), 100_000.0)
        self.assertLess(abs(moved.adjustment.north_m), 100_000.0)

    def test_high_latitude_antimeridian_adjustment_remains_in_requested_enu_frame(self):
        anchor = KmlCoordinate(179.9999, 82.0, 0.0)
        frame = LocalEnuFrame(anchor.latitude, anchor.longitude)
        start = _point(frame, 250.0, 400.0, 10.0)
        document = KmlDocument(
            name=None,
            styles=(KmlStyle("path", "ffffffff", 1.0),),
            placemarks=(
                KmlPlacemark(
                    "Path",
                    "#path",
                    KmlLineString((start, start), "absolute"),
                ),
            ),
        )

        adjusted = apply_enu_adjustment(
            document,
            anchor,
            TraceAdjustment(east_m=1_000.0, north_m=-500.0),
        )
        point = adjusted.placemarks[0].geometry.coordinates[0]
        position = frame.to_enu(point.latitude, point.longitude)

        self.assertAlmostEqual(position.east_m, 1_250.0, places=4)
        self.assertAlmostEqual(position.north_m, -100.0, places=4)
        self.assertTrue(-180.0 <= point.longitude <= 180.0)


class CanonicalPayloadTests(unittest.TestCase):
    def test_quantization_matches_export_precision_and_closes_polygon_once(self):
        canonical = quantize_kml_document(_document(open_polygon=True))

        line = canonical.placemarks[0].geometry
        polygon = canonical.placemarks[1].geometry
        self.assertEqual(line.coordinates[0].altitude_m, 100.124)
        self.assertEqual(len(polygon.outer_ring), 4)
        self.assertEqual(polygon.outer_ring[0], polygon.outer_ring[-1])

        reparsed = ET.fromstring(render_kml(canonical))
        serialized_line = reparsed.find(
            "kml:Document/kml:Placemark[1]/kml:LineString/kml:coordinates",
            NAMESPACE,
        ).text.splitlines()
        canonical_line = [
            f"{point.longitude:.7f},{point.latitude:.7f},{point.altitude_m:.3f}"
            for point in line.coordinates
        ]
        self.assertEqual(serialized_line, canonical_line)

    def test_payload_uses_canonical_coordinates_styles_and_altitude_modes(self):
        trace = PreparedTrace(
            "route-1",
            "Route 1",
            ANCHOR,
            _document(open_polygon=True),
            TraceAdjustment(east_m=12.3, up_m=4.5),
            anchor_altitude_mode="absolute",
        )

        payload = preview_payload(PreviewScene((trace,)))

        payload_trace = payload["traces"][0]
        line_payload, polygon_payload = payload_trace["geometries"]
        line = trace.adjusted_document.placemarks[0].geometry
        polygon = trace.adjusted_document.placemarks[1].geometry
        self.assertEqual(payload["version"], 2)
        self.assertEqual(payload_trace["id"], "route-1")
        self.assertEqual(payload_trace["anchor"]["label"], "Trace anchor")
        self.assertEqual(payload_trace["anchor"]["color"], "#ff00ffff")
        self.assertEqual(payload_trace["anchor"]["altitudeMode"], "ABSOLUTE")
        self.assertAlmostEqual(
            payload_trace["anchor"]["lat"],
            round(trace.adjusted_anchor.latitude, 7),
        )
        self.assertAlmostEqual(
            payload_trace["anchor"]["lng"],
            round(trace.adjusted_anchor.longitude, 7),
        )
        self.assertEqual(payload_trace["anchor"]["altitude"], 84.5)
        self.assertEqual(line_payload["id"], "geometry-0")
        self.assertEqual(polygon_payload["id"], "geometry-1")
        self.assertEqual(line_payload["type"], "polyline")
        self.assertEqual(line_payload["altitudeMode"], "ABSOLUTE")
        self.assertTrue(line_payload["extrude"])
        self.assertEqual(line_payload["style"]["strokeColor"], "#ff00ffaa")
        self.assertEqual(
            line_payload["coordinates"][0],
            {
                "lat": line.coordinates[0].latitude,
                "lng": line.coordinates[0].longitude,
                "altitude": line.coordinates[0].altitude_m,
            },
        )
        self.assertEqual(polygon_payload["type"], "polygon")
        self.assertEqual(polygon_payload["altitudeMode"], "RELATIVE_TO_GROUND")
        self.assertEqual(polygon_payload["style"]["strokeColor"], "#112233ff")
        self.assertEqual(polygon_payload["style"]["strokeWidth"], 2.501)
        self.assertEqual(polygon_payload["style"]["fillColor"], "#20304080")
        self.assertEqual(len(polygon_payload["coordinates"]), len(polygon.outer_ring))

    def test_payload_anchor_uses_relative_mode_when_a_clamped_trace_is_lifted(self):
        trace = PreparedTrace(
            "route-1",
            "Route 1",
            ANCHOR,
            _document(),
            TraceAdjustment(up_m=15.0),
        )

        anchor = preview_payload(PreviewScene((trace,)))["traces"][0]["anchor"]

        self.assertEqual(anchor["altitude"], 15.0)
        self.assertEqual(anchor["altitudeMode"], "RELATIVE_TO_GROUND")

    def test_colour_conversion_is_exact_and_rejects_invalid_input(self):
        self.assertEqual(kml_colour_to_css("aaff00ff"), "#ff00ffaa")
        self.assertEqual(kml_colour_to_css("80403020"), "#20304080")
        with self.assertRaisesRegex(ValueError, "aabbggrr"):
            kml_colour_to_css("#ff00ff")

    def test_scene_requires_unique_nonempty_trace_ids(self):
        trace = PreparedTrace("same", "One", ANCHOR, _document())
        with self.assertRaisesRegex(ValueError, "unique"):
            PreviewScene((trace, trace))
        with self.assertRaisesRegex(ValueError, "at least one"):
            PreviewScene(())


if __name__ == "__main__":
    unittest.main()
