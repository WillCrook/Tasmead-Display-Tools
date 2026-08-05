import math
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from services.geodesy import (
    EnuCoordinate,
    LocalEnuFrame,
    destination_point,
    inverse_distance_bearing,
    transpose_wgs84_enu_points,
)
from services.runway_alignment import RunwayReference, transpose_geodesic_points


class Wgs84GeodesicTests(unittest.TestCase):
    def test_inverse_matches_published_wgs84_reference(self):
        distance, bearing = inverse_distance_bearing(
            -41.32,
            174.81,
            40.96,
            -5.50,
        )

        self.assertAlmostEqual(distance, 19_959_679.267_353_82, places=6)
        self.assertAlmostEqual(bearing, 161.067_669_986_158_82, places=9)

    def test_direct_reconstructs_published_wgs84_endpoint(self):
        latitude, longitude = destination_point(
            -41.32,
            174.81,
            19_959_679.267_353_82,
            161.067_669_986_158_82,
        )

        self.assertAlmostEqual(latitude, 40.96, places=9)
        self.assertAlmostEqual(longitude, -5.50, places=9)

    def test_coincident_points_have_zero_distance_and_bearing(self):
        self.assertEqual(
            inverse_distance_bearing(51.0, -1.0, 51.0, -1.0),
            (0.0, 0.0),
        )

    def test_direct_projection_normalizes_antimeridian(self):
        latitude, longitude = destination_point(0.0, 179.9, 50_000.0, 90.0)

        self.assertAlmostEqual(latitude, 0.0, places=9)
        self.assertTrue(-180.0 <= longitude <= 180.0)
        self.assertLess(longitude, -179.0)

    def test_invalid_or_non_finite_inputs_fail_explicitly(self):
        cases = (
            lambda: inverse_distance_bearing(91.0, 0.0, 0.0, 0.0),
            lambda: inverse_distance_bearing(0.0, float("nan"), 0.0, 0.0),
            lambda: destination_point(0.0, 0.0, float("inf"), 90.0),
            lambda: destination_point(0.0, 0.0, 10.0, float("nan")),
        )
        for operation in cases:
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                operation()


class LocalEnuFrameTests(unittest.TestCase):
    def test_anchor_is_the_enu_origin(self):
        frame = LocalEnuFrame(51.0, -1.0)

        position = frame.to_enu(51.0, -1.0)

        self.assertAlmostEqual(position.east_m, 0.0, places=8)
        self.assertAlmostEqual(position.north_m, 0.0, places=8)
        self.assertAlmostEqual(position.up_m, 0.0, places=8)

    def test_round_trip_covers_hemispheres_high_latitude_and_antimeridian(self):
        cases = (
            ((51.0, -1.0), (51.002, -0.997)),
            ((-33.9, 151.2), (-33.897, 151.198)),
            ((82.0, 35.0), (82.001, 35.01)),
            ((0.0, 179.95), (0.01, -179.95)),
        )
        for origin, point in cases:
            with self.subTest(origin=origin, point=point):
                frame = LocalEnuFrame(*origin)
                restored = frame.to_wgs84(frame.to_enu(*point))
                self.assertAlmostEqual(restored[0], point[0], places=9)
                self.assertAlmostEqual(restored[1], point[1], places=9)

    def test_cardinal_geodesic_offsets_align_with_enu_axes(self):
        frame = LocalEnuFrame(51.0, -1.0)
        east_point = destination_point(51.0, -1.0, 1_000.0, 90.0)
        north_point = destination_point(51.0, -1.0, 1_000.0, 0.0)

        east = frame.to_enu(*east_point)
        north = frame.to_enu(*north_point)

        self.assertAlmostEqual(east.east_m, 1_000.0, places=5)
        self.assertAlmostEqual(east.north_m, 0.0, places=5)
        self.assertAlmostEqual(north.east_m, 0.0, places=5)
        self.assertAlmostEqual(north.north_m, 1_000.0, places=5)

    def test_invalid_enu_values_fail_explicitly(self):
        with self.assertRaises(ValueError):
            LocalEnuFrame(float("nan"), 0.0)
        with self.assertRaises(ValueError):
            EnuCoordinate(0.0, math.inf, 0.0)


class Wgs84EnuTranspositionTests(unittest.TestCase):
    def test_rotation_translation_preserve_local_shape_and_altitude(self):
        source_origin = (51.0, -1.0)
        target_origin = (-33.9, 151.2)
        source_frame = LocalEnuFrame(*source_origin)
        target_frame = LocalEnuFrame(*target_origin)
        source_positions = (
            EnuCoordinate(0.0, 0.0, 0.0),
            EnuCoordinate(100.0, 800.0, -0.05),
            EnuCoordinate(-250.0, 1_200.0, -0.12),
        )
        altitudes = (25.0, 125.0, -5.0)
        points = tuple(
            (*source_frame.to_wgs84(position), altitude)
            for position, altitude in zip(source_positions, altitudes, strict=True)
        )

        transformed = transpose_wgs84_enu_points(
            points,
            source_origin,
            target_origin,
            90.0,
        )

        for source, transformed_point in zip(
            source_positions,
            transformed,
            strict=True,
        ):
            target = target_frame.to_enu(*transformed_point[:2])
            self.assertAlmostEqual(target.east_m, source.north_m, places=5)
            self.assertAlmostEqual(target.north_m, -source.east_m, places=5)
            expected = target_frame.to_wgs84(
                EnuCoordinate(source.north_m, -source.east_m, source.up_m)
            )
            self.assertAlmostEqual(transformed_point[0], expected[0], places=12)
            self.assertAlmostEqual(transformed_point[1], expected[1], places=12)
        self.assertEqual(tuple(point[2] for point in transformed), altitudes)

    def test_compatibility_wrapper_matches_primary_enu_operation(self):
        source = RunwayReference(51.0, -1.0, 90.0)
        target = RunwayReference(-33.9, 151.2, 180.0)
        point = destination_point(51.0, -1.0, 750.0, 100.0)
        points = ((*point, 42.0),)

        primary = transpose_wgs84_enu_points(
            points,
            (source.latitude, source.longitude),
            (target.latitude, target.longitude),
            target.true_heading_deg - source.true_heading_deg,
        )

        self.assertEqual(transpose_geodesic_points(points, source, target), primary)

    def test_non_finite_route_altitude_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Route altitude"):
            transpose_wgs84_enu_points(
                ((51.0, -1.0, float("nan")),),
                (51.0, -1.0),
                (52.0, 0.0),
                0.0,
            )


if __name__ == "__main__":
    unittest.main()
