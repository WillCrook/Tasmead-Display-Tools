from __future__ import annotations

import sys
import math
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "kml"

from services.kml_editor_operations import (
    KmlEditorOperationCancelled,
    apply_retained_indices,
    build_crop_preview_scene,
    build_simplification_preview_scene,
    crop_indices,
    simplify_track,
    timestamp_info,
)
from services.kml_file_handling import KmlPoint, KmlTrack, parse_kml_text
from services.geodesy import LocalEnuFrame


class KmlEditorSourceOperationTests(unittest.TestCase):
    def test_line_string_crop_changes_only_coordinate_body_and_preserves_companion(self):
        source = (FIXTURES / "editor_operations_line_string.kml").read_text()
        track = parse_kml_text(source, source_name="editor_operations_line_string.kml")

        result = apply_retained_indices(source, track, crop_indices(track, 1, 3))

        self.assertIn("0.001,51.0001,102 0.002,51.0005,160 0.003,51.0001,104", result.contents)
        self.assertNotIn("0,51,100 ", result.contents)
        self.assertIn("<!-- This comment, style and point must survive path edits exactly. -->", result.contents)
        self.assertIn("<Point><coordinates>0,51,0</coordinates></Point>", result.contents)
        self.assertIn("Preserved 1 Point feature", result.warnings[0])
        reparsed = parse_kml_text(result.contents)
        self.assertEqual(len(reparsed.points), 3)

    def test_gx_crop_filters_only_exactly_aligned_per_point_series(self):
        source = (FIXTURES / "editor_operations_gx_track.kml").read_text()
        track = parse_kml_text(source)

        result = apply_retained_indices(source, track, (0, 2))

        self.assertNotIn("0.001 51 100", result.contents)
        self.assertNotIn("2026-01-01T00:00:01Z", result.contents)
        self.assertNotIn("<gx:angles>1 0 0</gx:angles>", result.contents)
        self.assertNotIn("<gx:value>20</gx:value>", result.contents)
        self.assertIn("<gx:value>10</gx:value>", result.contents)
        self.assertIn("<gx:value>30</gx:value>", result.contents)
        self.assertEqual(len(parse_kml_text(result.contents).points), 2)

    def test_gx_arrays_outside_the_selected_geometry_are_never_modified(self):
        source = """<kml xmlns="http://www.opengis.net/kml/2.2"
 xmlns:gx="http://www.google.com/kml/ext/2.2"><Document>
 <Placemark><gx:Track>
  <when>2026-01-01T00:00:00Z</when><when>2026-01-01T00:00:01Z</when><when>2026-01-01T00:00:02Z</when>
  <gx:coord>0 51 10</gx:coord><gx:coord>0.1 51 20</gx:coord><gx:coord>0.2 51 30</gx:coord>
 </gx:Track></Placemark>
 <gx:Track><ExtendedData><gx:SimpleArrayData name="unrelated">
  <gx:value>999</gx:value><gx:value>998</gx:value><gx:value>997</gx:value>
 </gx:SimpleArrayData></ExtendedData></gx:Track>
 </Document></kml>"""
        track = parse_kml_text(source)

        result = apply_retained_indices(source, track, (0, 2))

        self.assertIn("<gx:value>999</gx:value>", result.contents)
        self.assertIn("<gx:value>998</gx:value>", result.contents)
        self.assertIn("<gx:value>997</gx:value>", result.contents)

    def test_cdata_coordinate_body_is_valid_but_apply_is_refused(self):
        source = (
            '<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><LineString>'
            '<coordinates><![CDATA[0,51,1 1,52,2]]></coordinates>'
            '</LineString></Placemark></kml>'
        )
        track = parse_kml_text(source)
        self.assertIsNotNone(track.source_binding.unsafe_reason)
        with self.assertRaisesRegex(ValueError, "CDATA"):
            apply_retained_indices(source, track, (0, 1))


class RamerDouglasPeuckerTests(unittest.TestCase):
    def track(self, points, *, altitude_mode="clampToGround", timestamps=None):
        timestamps = timestamps or (None,) * len(points)
        return KmlTrack(
            tuple(
                KmlPoint(latitude, longitude, altitude, timestamp)
                for (latitude, longitude, altitude), timestamp in zip(points, timestamps, strict=True)
            ),
            "gx_track" if any(timestamps) else "line_string",
            "Track",
            altitude_mode,
        )

    def test_endpoints_and_turns_are_retained_while_straight_points_reduce(self):
        straight = self.track(((51, 0, None), (51, .001, None), (51, .002, None)))
        self.assertEqual(simplify_track(straight, 1).kept_indices, (0, 2))
        turn = self.track(((51, 0, None), (51.001, .001, None), (51, .002, None)))
        self.assertEqual(simplify_track(turn, 20).kept_indices, (0, 1, 2))

    def test_altitude_is_meaningful_except_for_clamp_to_ground(self):
        points = ((51, 0, 100), (51, .001, 150), (51, .002, 100))
        absolute = self.track(points, altitude_mode="absolute")
        clamped = self.track(points, altitude_mode="clampToGround")
        self.assertEqual(simplify_track(absolute, 20).kept_indices, (0, 1, 2))
        self.assertEqual(simplify_track(clamped, 20).kept_indices, (0, 2))

    def test_reliable_timing_preserves_speed_profile_and_bad_timing_falls_back(self):
        points = ((51, 0, 100), (51, .001, 100), (51, .002, 100))
        reliable = self.track(
            points,
            altitude_mode="absolute",
            timestamps=(
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:01Z",
                "2026-01-01T00:00:10Z",
            ),
        )
        unreliable = self.track(
            points,
            altitude_mode="absolute",
            timestamps=("bad", "bad", "bad"),
        )
        self.assertTrue(timestamp_info(reliable).reliable)
        self.assertEqual(simplify_track(reliable, 5).kept_indices, (0, 1, 2))
        self.assertFalse(timestamp_info(unreliable).reliable)
        self.assertEqual(simplify_track(unreliable, 5).kept_indices, (0, 2))

    def test_presets_are_metric_and_preview_contains_complete_comparisons(self):
        source = (FIXTURES / "editor_operations_line_string.kml").read_text()
        track = parse_kml_text(source)
        counts = [simplify_track(track, tolerance).result_count for tolerance in (1, 5, 20)]
        self.assertGreaterEqual(counts[0], counts[1])
        self.assertGreaterEqual(counts[1], counts[2])
        result = simplify_track(track, 5)
        crop_scene = build_crop_preview_scene(track, 1, 3, trace_id="crop", label="crop")
        reduce_scene = build_simplification_preview_scene(track, result, trace_id="rdp", label="rdp")
        self.assertEqual(len(crop_scene.traces[0].base_document.placemarks), 3)
        self.assertEqual(len(reduce_scene.traces[0].base_document.placemarks), 2)
        original = reduce_scene.traces[0].base_document.placemarks[0].geometry.coordinates
        self.assertEqual(len(original), len(track.points))

    def test_tolerance_must_be_positive(self):
        track = self.track(((51, 0, None), (51, .001, None)))
        for value in (0, -1, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                simplify_track(track, value)

    def test_antimeridian_and_fifty_kilometre_section_boundaries_are_safe(self):
        antimeridian = self.track(
            ((0, 179.9, None), (0, -179.9, None), (0, -179.7, None))
        )
        result = simplify_track(antimeridian, 20)
        self.assertEqual((result.kept_indices[0], result.kept_indices[-1]), (0, 2))
        long_track = self.track(((0, 0, None), (0, .6, None), (0, 1.2, None)))
        self.assertEqual(simplify_track(long_track, 100000).kept_indices, (0, 1, 2))

    def test_cooperative_cancellation_stops_rdp(self):
        track = self.track(tuple((51, index / 10000, None) for index in range(100)))
        with self.assertRaises(KmlEditorOperationCancelled):
            simplify_track(track, 1, cancellation_check=lambda: True)

    def test_every_removed_vertex_respects_the_metric_tolerance(self):
        track = self.track(
            tuple(
                (51 + index * 0.00005, -1 + index * 0.00005 + math.sin(index / 4) * 0.00001, None)
                for index in range(80)
            )
        )
        tolerance = 1.0
        result = simplify_track(track, tolerance)
        for first_index, last_index in zip(result.kept_indices, result.kept_indices[1:]):
            anchor = track.points[(first_index + last_index) // 2]
            frame = LocalEnuFrame(anchor.latitude, anchor.longitude)
            positions = frame.to_enu_many(
                (point.latitude, point.longitude)
                for point in track.points[first_index : last_index + 1]
            )
            start = positions[0]
            end = positions[-1]
            vx, vy = end.east_m - start.east_m, end.north_m - start.north_m
            denominator = vx * vx + vy * vy
            for point in positions[1:-1]:
                fraction = 0.0 if denominator == 0 else max(
                    0.0,
                    min(
                        1.0,
                        ((point.east_m - start.east_m) * vx + (point.north_m - start.north_m) * vy)
                        / denominator,
                    ),
                )
                deviation = math.hypot(
                    point.east_m - (start.east_m + fraction * vx),
                    point.north_m - (start.north_m + fraction * vy),
                )
                self.assertLessEqual(deviation, tolerance + 1e-6)

    def test_crop_and_reduction_compose_only_in_explicit_apply_order(self):
        source = (FIXTURES / "editor_operations_line_string.kml").read_text()
        original = parse_kml_text(source)
        cropped_text = apply_retained_indices(source, original, crop_indices(original, 1, 4)).contents
        cropped = parse_kml_text(cropped_text)
        # Each second operation reparses and indexes the first operation's output;
        # no unapplied range is implicitly consumed.
        cropped_reduction = simplify_track(cropped, 20)
        cropped_then_reduced = apply_retained_indices(
            cropped_text,
            cropped,
            cropped_reduction.kept_indices,
        ).contents
        reduced = simplify_track(original, 20)
        reduced_text = apply_retained_indices(source, original, reduced.kept_indices).contents
        reduced_track = parse_kml_text(reduced_text)
        if len(reduced_track.points) > 2:
            reduced_then_cropped = apply_retained_indices(
                reduced_text,
                reduced_track,
                crop_indices(reduced_track, 0, len(reduced_track.points) - 2),
            ).contents
        else:
            reduced_then_cropped = reduced_text
        self.assertEqual(len(parse_kml_text(cropped_then_reduced).points), cropped_reduction.result_count)
        self.assertLessEqual(len(parse_kml_text(reduced_then_cropped).points), len(reduced_track.points))


if __name__ == "__main__":
    unittest.main()
