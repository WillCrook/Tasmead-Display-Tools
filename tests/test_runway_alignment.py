import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from services import (
    DetectionSignalScore,
    KmlPoint,
    KmlTrack,
    RunwayConfidence,
    RunwayReference,
    destination_point,
    infer_departure_runway,
    inverse_distance_bearing,
    transpose_geodesic_points,
)
from services.runway_alignment import _interpolated_score
from services.transpose_coordinates import _waypoints_for_transposition


def runway_track(
    heading=90.0,
    *,
    latitude=51.0,
    longitude=-1.0,
    distance_m=1_200,
    altitude_mode="absolute",
):
    points = [
        KmlPoint(latitude, longitude, 100.0),
        KmlPoint(latitude, longitude, 100.1),
    ]
    for distance in range(0, distance_m + 1, 50):
        point_latitude, point_longitude = destination_point(
            latitude,
            longitude,
            distance,
            heading,
        )
        points.append(
            KmlPoint(
                point_latitude,
                point_longitude,
                100.0 + distance * 0.04,
            )
        )
    return KmlTrack(
        points=tuple(points),
        geometry_kind="line_string",
        placemark_name="Synthetic departure",
        altitude_mode=altitude_mode,
    )


def elevation_profile_track(
    altitude_for_distance,
    *,
    distances=range(0, 1_201, 25),
    heading=90.0,
    altitude_mode="absolute",
):
    points = []
    for distance in distances:
        latitude, longitude = destination_point(51.0, -1.0, distance, heading)
        points.append(
            KmlPoint(
                latitude,
                longitude,
                altitude_for_distance(distance),
            )
        )
    return KmlTrack(
        points=tuple(points),
        geometry_kind="line_string",
        placemark_name="Elevation profile",
        altitude_mode=altitude_mode,
    )


class RunwayReferenceTests(unittest.TestCase):
    def test_heading_is_finite_validated_and_normalized(self):
        self.assertEqual(RunwayReference(51, -1, 450).true_heading_deg, 90.0)
        for value in (float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                RunwayReference(51, -1, value)

    def test_threshold_maps_exactly_and_heading_rotation_preserves_distance(self):
        source = RunwayReference(51.0, -1.0, 90.0)
        target = RunwayReference(-33.9, 151.2, 180.0)
        source_point = destination_point(source.latitude, source.longitude, 1_000, 90)
        transformed = transpose_geodesic_points(
            (
                (source.latitude, source.longitude, 25.0),
                (*source_point, 125.0),
            ),
            source,
            target,
        )

        self.assertAlmostEqual(transformed[0][0], target.latitude, places=9)
        self.assertAlmostEqual(transformed[0][1], target.longitude, places=9)
        distance, bearing = inverse_distance_bearing(
            target.latitude,
            target.longitude,
            transformed[1][0],
            transformed[1][1],
        )
        self.assertAlmostEqual(distance, 1_000, places=5)
        self.assertAlmostEqual(bearing, 180.0, places=5)
        self.assertEqual([point[2] for point in transformed], [25.0, 125.0])

    def test_antimeridian_projection_normalizes_longitude(self):
        latitude, longitude = destination_point(0.0, 179.9, 50_000, 90.0)
        self.assertAlmostEqual(latitude, 0.0, places=6)
        self.assertTrue(-180.0 <= longitude <= 180.0)
        self.assertLess(longitude, -179.0)


class DepartureInferenceTests(unittest.TestCase):
    def test_stationary_jitter_is_ignored_and_sustained_heading_is_fitted(self):
        result = infer_departure_runway(runway_track(heading=90.0))

        self.assertIsNotNone(result.candidate)
        self.assertIs(result.candidate.confidence, RunwayConfidence.LOW)
        self.assertLess(
            abs((result.candidate.reference.true_heading_deg - 90.0 + 180) % 360 - 180),
            2.0,
        )
        self.assertGreaterEqual(result.candidate.aligned_distance_m, 700.0)
        self.assertIn("Ignored stationary jitter points", result.candidate.evidence[3])
        self.assertIs(result.candidate.heading_confidence, RunwayConfidence.HIGH)
        self.assertIs(result.candidate.threshold_confidence, RunwayConfidence.HIGH)
        assessment = result.candidate.detection_assessment
        self.assertEqual(assessment.overall_percent, 59)
        self.assertEqual(assessment.ground_elevation_percent, 0)
        self.assertIn("required ground elevation", assessment.cap_reason)

    def test_departure_after_long_apron_recording_is_still_inferred(self):
        runway_latitude = 48.9489
        runway_longitude = 2.4270
        apron_latitude, apron_longitude = destination_point(
            runway_latitude,
            runway_longitude,
            150.0,
            270.0,
        )
        points = []
        for angle in range(0, 720, 6):
            latitude, longitude = destination_point(
                apron_latitude,
                apron_longitude,
                25.0,
                angle,
            )
            points.append(KmlPoint(latitude, longitude, 40.0))
        for distance in range(0, 1_201, 50):
            latitude, longitude = destination_point(
                runway_latitude,
                runway_longitude,
                distance,
                25.0,
            )
            points.append(
                KmlPoint(latitude, longitude, 40.0 + max(0, distance - 450) * 0.08)
            )
        track = KmlTrack(
            points=tuple(points),
            geometry_kind="line_string",
            placemark_name="Delayed departure",
            altitude_mode="absolute",
        )

        result = infer_departure_runway(track)

        self.assertIsNotNone(result.candidate)
        candidate = result.candidate
        difference = abs(
            (candidate.reference.true_heading_deg - 25.0 + 180) % 360 - 180
        )
        threshold_error, _ = inverse_distance_bearing(
            runway_latitude,
            runway_longitude,
            candidate.reference.latitude,
            candidate.reference.longitude,
        )
        self.assertLess(difference, 2.0)
        self.assertLess(threshold_error, 50.0)
        self.assertGreater(candidate.start_index, len(track.points) * 0.35)
        self.assertIs(candidate.heading_confidence, RunwayConfidence.HIGH)
        self.assertIn("climb confirmed", candidate.evidence[-1])

    def test_climbing_event_is_preferred_over_later_descending_runway_event(self):
        departure = runway_track(heading=75.0, distance_m=900)
        points = list(departure.points)
        arrival_start_latitude, arrival_start_longitude = destination_point(
            51.0,
            -1.0,
            5_000.0,
            180.0,
        )
        for distance in range(0, 1_401, 50):
            latitude, longitude = destination_point(
                arrival_start_latitude,
                arrival_start_longitude,
                distance,
                240.0,
            )
            points.append(KmlPoint(latitude, longitude, 220.0 - distance * 0.08))
        track = KmlTrack(
            points=tuple(points),
            geometry_kind="line_string",
            placemark_name="Departure and arrival",
            altitude_mode="absolute",
        )

        result = infer_departure_runway(track)

        self.assertIsNotNone(result.candidate)
        difference = abs(
            (result.candidate.reference.true_heading_deg - 75.0 + 180) % 360
            - 180
        )
        self.assertLess(difference, 3.0)
        self.assertIn("climb confirmed", result.candidate.evidence[-1])

    def test_later_opposite_direction_does_not_replace_departure(self):
        departure = runway_track(heading=130.0)
        points = list(departure.points)
        for distance in range(0, 1_200, 50):
            latitude, longitude = destination_point(51.0, -1.0, distance, 310.0)
            points.extend([KmlPoint(latitude, longitude, 100.0)] * 3)
        track = KmlTrack(
            points=tuple(points),
            geometry_kind="line_string",
            placemark_name="Out and back",
            altitude_mode="absolute",
        )

        result = infer_departure_runway(track)

        self.assertIsNotNone(result.candidate)
        difference = abs(
            (result.candidate.reference.true_heading_deg - 130.0 + 180) % 360 - 180
        )
        self.assertLess(difference, 3.0)

    def test_level_runway_pass_does_not_override_later_climbing_departure(self):
        points = []
        for distance in range(0, 1_001, 50):
            latitude, longitude = destination_point(51.0, -1.0, distance, 130.0)
            points.append(KmlPoint(latitude, longitude, 100.0))
        second_threshold_latitude, second_threshold_longitude = destination_point(
            51.0,
            -1.0,
            3_000.0,
            270.0,
        )
        for distance in range(0, 1_001, 50):
            latitude, longitude = destination_point(
                second_threshold_latitude,
                second_threshold_longitude,
                distance,
                310.0,
            )
            points.append(KmlPoint(latitude, longitude, 100.0 + distance * 0.05))
        track = KmlTrack(
            points=tuple(points),
            geometry_kind="line_string",
            placemark_name="Runway pass then departure",
            altitude_mode="absolute",
        )

        result = infer_departure_runway(track)

        self.assertIsNotNone(result.candidate)
        difference = abs(
            (result.candidate.reference.true_heading_deg - 310.0 + 180) % 360
            - 180
        )
        self.assertLess(difference, 3.0)
        self.assertIn("climb confirmed", result.candidate.evidence[-1])

    def test_stationary_track_requires_manual_entry(self):
        track = KmlTrack(
            points=(
                KmlPoint(51.0, -1.0, 100.0),
                KmlPoint(51.0, -1.0, 100.0),
                KmlPoint(51.0, -1.0, 100.0),
            ),
            geometry_kind="line_string",
            placemark_name="Stationary",
            altitude_mode="absolute",
        )
        result = infer_departure_runway(track)
        self.assertIsNone(result.candidate)
        self.assertIn("no movement", result.error)


class GroundElevationInferenceTests(unittest.TestCase):
    def test_detection_score_interpolation_uses_declared_boundaries(self):
        anchors = (
            (300.0, 0.0),
            (400.0, 60.0),
            (700.0, 85.0),
            (800.0, 100.0),
        )
        self.assertEqual(_interpolated_score(250.0, anchors), 0.0)
        self.assertEqual(_interpolated_score(400.0, anchors), 60.0)
        self.assertEqual(_interpolated_score(700.0, anchors), 85.0)
        self.assertEqual(_interpolated_score(900.0, anchors), 100.0)

    def test_isolated_low_outlier_does_not_control_ground_elevation(self):
        def altitude(distance):
            if distance == 100:
                return 35.0
            return 100.0 if distance <= 500 else 100.0 + (distance - 500) * 0.08

        result = infer_departure_runway(
            elevation_profile_track(altitude),
            fallback_elevation_m=70.0,
        )

        self.assertIsNotNone(result.candidate)
        self.assertAlmostEqual(result.candidate.reference.elevation_m, 100.0)
        self.assertTrue(
            any("stable sample" in item for item in result.candidate.evidence)
        )
        self.assertFalse(
            any("preset fallback" in item for item in result.candidate.evidence)
        )

    def test_climb_samples_are_not_averaged_into_ground_reference(self):
        result = infer_departure_runway(
            elevation_profile_track(
                lambda distance: (
                    80.0
                    if distance <= 300
                    else 80.0 + (distance - 300) * 0.08
                )
            )
        )

        self.assertIsNotNone(result.candidate)
        self.assertAlmostEqual(result.candidate.reference.elevation_m, 80.0)

    def test_negative_sloped_runway_uses_lowest_stable_window_median(self):
        result = infer_departure_runway(
            elevation_profile_track(
                lambda distance: (
                    -10.0 + distance * 0.01
                    if distance <= 500
                    else -5.0 + (distance - 500) * 0.08
                )
            )
        )

        self.assertIsNotNone(result.candidate)
        self.assertGreaterEqual(result.candidate.reference.elevation_m, -10.0)
        self.assertLess(result.candidate.reference.elevation_m, -9.0)
        self.assertTrue(
            any("+1.00% slope" in item for item in result.candidate.evidence)
        )

    def test_five_sparse_ground_samples_can_form_stable_window(self):
        ground_altitudes = {
            0: 98.0,
            80: 98.9,
            160: 100.1,
            240: 100.6,
            320: 101.2,
        }
        distances = (0, 80, 160, 240, 320, 500, 650, 800, 950, 1_100)

        result = infer_departure_runway(
            elevation_profile_track(
                lambda distance: ground_altitudes.get(
                    distance,
                    101.2 + (distance - 320) * 0.08,
                ),
                distances=distances,
            )
        )

        self.assertIsNotNone(result.candidate)
        self.assertAlmostEqual(result.candidate.reference.elevation_m, 100.1)
        self.assertTrue(
            any("5 stable sample(s)" in item for item in result.candidate.evidence)
        )

    def test_unstable_altitudes_use_fallback_or_require_manual_elevation(self):
        def unstable_altitude(distance):
            noise = 20.0 if (distance // 25) % 2 else 0.0
            climb = max(0, distance - 500) * 0.08
            return 100.0 + noise + climb

        track = elevation_profile_track(unstable_altitude)
        fallback = infer_departure_runway(track, fallback_elevation_m=88.0)
        manual = infer_departure_runway(track)

        self.assertIsNotNone(fallback.candidate)
        self.assertEqual(fallback.candidate.reference.elevation_m, 88.0)
        self.assertTrue(
            any("preset fallback" in item for item in fallback.candidate.evidence)
        )
        self.assertTrue(any("verify the preset fallback" in item for item in fallback.warnings))
        self.assertIsNotNone(manual.candidate)
        self.assertIsNone(manual.candidate.reference.elevation_m)
        self.assertTrue(any("enter it manually" in item for item in manual.warnings))
        fallback_assessment = fallback.candidate.detection_assessment
        manual_assessment = manual.candidate.detection_assessment
        self.assertEqual(fallback_assessment.ground_elevation_percent, 40)
        self.assertEqual(fallback_assessment.overall_percent, 79)
        self.assertIs(fallback_assessment.rating, RunwayConfidence.MEDIUM)
        self.assertEqual(manual_assessment.ground_elevation_percent, 0)
        self.assertEqual(manual_assessment.overall_percent, 59)

    def test_non_absolute_track_does_not_infer_ground_reference(self):
        result = infer_departure_runway(
            elevation_profile_track(
                lambda distance: 0.0 if distance <= 500 else (distance - 500) * 0.08,
                altitude_mode="relativeToGround",
            ),
            fallback_elevation_m=75.0,
        )

        self.assertIsNotNone(result.candidate)
        self.assertIsNone(result.candidate.reference.elevation_m)
        self.assertFalse(
            any("Ground reference elevation" in item for item in result.candidate.evidence)
        )
        assessment = result.candidate.detection_assessment
        self.assertEqual(assessment.ground_elevation_percent, 100)
        self.assertIs(assessment.rating, RunwayConfidence.HIGH)

    def test_clamped_ground_is_high_and_unsupported_sea_floor_is_low(self):
        clamped = infer_departure_runway(runway_track(altitude_mode="clampToGround"))
        sea_floor = infer_departure_runway(
            runway_track(altitude_mode="relativeToSeaFloor")
        )

        self.assertEqual(
            clamped.candidate.detection_assessment.ground_elevation_percent,
            100,
        )
        self.assertEqual(
            sea_floor.candidate.detection_assessment.ground_elevation_percent,
            0,
        )
        self.assertIs(
            sea_floor.candidate.detection_assessment.rating,
            RunwayConfidence.MEDIUM,
        )
        self.assertEqual(
            sea_floor.candidate.detection_assessment.overall_percent,
            77,
        )

    def test_critically_weak_heading_signal_caps_weighted_result(self):
        signals = (
            DetectionSignalScore("Heading consistency", 18, "test signal"),
            DetectionSignalScore("Cross-track fit", 96, "test signal"),
            DetectionSignalScore("Straightness", 96, "test signal"),
            DetectionSignalScore("Aligned length", 93, "test signal"),
        )
        with patch(
            "services.runway_alignment._heading_detection_scores",
            return_value=(91.0, signals),
        ):
            result = infer_departure_runway(
                runway_track(altitude_mode="relativeToGround")
            )

        assessment = result.candidate.detection_assessment
        self.assertEqual(assessment.overall_percent, 59)
        self.assertIs(assessment.rating, RunwayConfidence.LOW)
        self.assertEqual(assessment.weakest_signal.name, "Heading consistency")
        self.assertIn("critically weak", assessment.cap_reason)


class AltitudeModeTests(unittest.TestCase):
    def points(self, mode, altitudes):
        return KmlTrack(
            points=tuple(
                KmlPoint(51.0 + index * 0.001, -1.0, altitude)
                for index, altitude in enumerate(altitudes)
            ),
            geometry_kind="line_string",
            placemark_name="Altitude test",
            altitude_mode=mode,
        )

    def test_absolute_altitude_omits_missing_values_and_reports_count(self):
        source = RunwayReference(51, -1, 90, 80)
        values, warnings = _waypoints_for_transposition(
            self.points("absolute", (None, 75.0, None, 100.0, None)),
            source,
        )
        self.assertEqual(
            values,
            [(51.001, -1.0, -5.0), (51.003, -1.0, 20.0)],
        )
        self.assertEqual(
            warnings,
            (
                "Omitted 3 source coordinate(s) because absolute altitude was missing.",
            ),
        )

    def test_absolute_altitude_fails_when_fewer_than_two_valid_points_remain(self):
        with self.assertRaisesRegex(
            ValueError,
            "Omitted 2 source coordinate.*fewer than two valid coordinates remain",
        ):
            _waypoints_for_transposition(
                self.points("absolute", (None, 100.0, None)),
                RunwayReference(51, -1, 90, 80),
            )

    def test_relative_and_clamped_altitudes_are_not_subtracted(self):
        source = RunwayReference(51, -1, 90, 80)
        relative, relative_warnings = _waypoints_for_transposition(
            self.points("relativeToGround", (5.0, None)),
            source,
        )
        clamped, clamped_warnings = _waypoints_for_transposition(
            self.points("clampToGround", (100.0, 200.0)),
            source,
        )
        self.assertEqual([point[2] for point in relative], [5.0, 0.0])
        self.assertEqual([point[2] for point in clamped], [0.0, 0.0])
        self.assertEqual(relative_warnings, ())
        self.assertEqual(clamped_warnings, ())

    def test_absolute_altitude_requires_reviewed_elevation(self):
        with self.assertRaisesRegex(ValueError, "elevation is required"):
            _waypoints_for_transposition(
                self.points("absolute", (100.0, 120.0)),
                RunwayReference(51, -1, 90),
            )

    def test_sea_floor_altitude_mode_is_not_silently_relabelled(self):
        with self.assertRaisesRegex(ValueError, "cannot be converted safely"):
            _waypoints_for_transposition(
                self.points("relativeToSeaFloor", (5.0, 10.0)),
                RunwayReference(51, -1, 90),
            )


if __name__ == "__main__":
    unittest.main()
