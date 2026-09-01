from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "kml"

from services.kml_export import KmlCoordinate, KmlLineString, render_kml
from services.map_preview import TraceAdjustment
from services.runway_alignment import RunwayReference
from services.transpose_coordinates import (
    ManualTranspositionAlignment,
    PreparedTranspositionBatch,
    TranspositionErrorCode,
    create_transposition_plan,
    export_prepared_transposition,
    prepare_transposition,
)


class TranspositionPreparationTests(unittest.TestCase):
    source_runway = RunwayReference(51.2, -0.7, 32.0, 0.0)
    target_runway = RunwayReference(51.0, -1.0, 90.0)

    def fixture(self, name: str) -> Path:
        return FIXTURES / name

    def prepare(self, filenames: tuple[str, ...]) -> PreparedTranspositionBatch:
        inputs = tuple(self.fixture(filename) for filename in filenames)
        with patch(
            "services.transpose_coordinates.transpose_wgs84_enu_points",
            side_effect=lambda waypoints, *_: tuple(waypoints),
        ):
            return prepare_transposition(
                input_files=inputs,
                source_runways=(self.source_runway,) * len(inputs),
                target_runway=self.target_runway,
            )

    def test_preview_first_preparation_builds_documents_without_writing(self):
        with patch("services.transpose_coordinates.write_kml") as write:
            batch = self.prepare(("line_string_namespaced.kml",))

        write.assert_not_called()
        self.assertEqual(batch.prepared_count, 1)
        self.assertEqual(batch.failure_count, 0)
        prepared = batch.prepared[0]
        self.assertEqual(
            prepared.trace.anchor,
            KmlCoordinate(longitude=-1.0, latitude=51.0, altitude_m=0.0),
        )
        self.assertEqual(prepared.trace.label, "line_string_namespaced")
        self.assertEqual(prepared.document.styles[0].line_colour, "aa00ffff")
        geometry = prepared.document.placemarks[0].geometry
        self.assertIsInstance(geometry, KmlLineString)
        self.assertEqual(geometry.altitude_mode, "relativeToGround")
        self.assertTrue(geometry.extrude_to_ground)

    def test_preview_trace_identity_is_stable_by_source_path_and_not_sensitive(self):
        source = self.fixture("line_string_namespaced.kml")
        first = self.prepare(("line_string_namespaced.kml",))
        reordered = self.prepare(("gx_track.kml", "line_string_namespaced.kml"))

        first_id = first.prepared[0].trace.trace_id
        reordered_id = next(
            item.trace.trace_id
            for item in reordered.prepared
            if item.input_path == source
        )

        self.assertEqual(first_id, reordered_id)
        self.assertRegex(first_id, r"\Atransposition-[0-9a-f]{24}\Z")
        self.assertNotIn(source.name, first_id)
        self.assertNotIn(str(source.parent), first_id)
        self.assertEqual(
            len({item.trace.trace_id for item in reordered.prepared}),
            reordered.prepared_count,
        )

    def test_preparation_failures_are_carried_into_later_export(self):
        inputs = (
            self.fixture("line_string_namespaced.kml"),
            self.fixture("wrong_arity.kml"),
        )
        batch = self.prepare(("line_string_namespaced.kml", "wrong_arity.kml"))
        self.assertEqual(batch.prepared_count, 1)
        self.assertEqual(batch.failure_count, 1)
        self.assertEqual(batch.failed_items[0].code, TranspositionErrorCode.INPUT_KML)

        with tempfile.TemporaryDirectory() as temp_dir:
            plan = create_transposition_plan(inputs, temp_dir, "Field")
            result = export_prepared_transposition(batch, plan)

            self.assertTrue(result.partially_succeeded)
            self.assertTrue(result.successful[0].output_path.is_file())
            self.assertEqual(
                result.failed_outcomes[0].error.code,
                TranspositionErrorCode.INPUT_KML,
            )
            self.assertFalse(result.failed_outcomes[0].planned_output_path.exists())

    def test_export_writes_the_exact_adjusted_preview_document(self):
        batch = self.prepare(("line_string_namespaced.kml",))
        prepared = batch.prepared[0]
        adjusted = replace(
            prepared,
            trace=prepared.trace.with_adjustment(
                TraceAdjustment(
                    east_m=12.3,
                    north_m=-4.5,
                    up_m=6.7,
                    yaw_deg=8.9,
                )
            ),
        )
        adjusted_batch = replace(batch, items=(adjusted,))

        with tempfile.TemporaryDirectory() as temp_dir:
            plan = create_transposition_plan(
                [prepared.input_path], temp_dir, "Field"
            )
            result = export_prepared_transposition(adjusted_batch, plan)

            self.assertTrue(result.succeeded)
            self.assertEqual(
                result.successful[0].output_path.read_text(encoding="utf-8"),
                render_kml(adjusted.document),
            )

    def test_export_rejects_reordered_inputs_before_writing_any_file(self):
        batch = self.prepare(("line_string_namespaced.kml", "gx_track.kml"))
        with tempfile.TemporaryDirectory() as temp_dir:
            plan = create_transposition_plan(
                [
                    self.fixture("gx_track.kml"),
                    self.fixture("line_string_namespaced.kml"),
                ],
                temp_dir,
                "Field",
            )
            with (
                patch("services.transpose_coordinates.write_kml") as write,
                self.assertRaisesRegex(ValueError, "same order"),
            ):
                export_prepared_transposition(batch, plan)

            write.assert_not_called()

    def test_duplicate_aircraft_names_have_unambiguous_preview_labels(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            first = Path(temp_dir) / "one" / "display.kml"
            second = Path(temp_dir) / "two" / "display.kml"
            for path in (first, second):
                path.parent.mkdir()
                path.write_bytes(self.fixture("line_string_namespaced.kml").read_bytes())
            with patch(
                "services.transpose_coordinates.transpose_wgs84_enu_points",
                side_effect=lambda waypoints, *_: tuple(waypoints),
            ):
                batch = prepare_transposition(
                    input_files=(first, second),
                    source_runways=(self.source_runway, self.source_runway),
                    target_runway=self.target_runway,
                )

        labels = tuple(item.trace.label for item in batch.prepared)
        self.assertEqual(len(set(labels)), 2)
        self.assertTrue(all(label.startswith("display — ") for label in labels))

    def test_manual_alignment_uses_first_point_target_and_clockwise_delta(self):
        source = self.fixture("line_string_namespaced.kml")
        alignment = ManualTranspositionAlignment(52.0, 0.25, 35.0)

        with patch(
            "services.transpose_coordinates.transpose_wgs84_enu_points",
            side_effect=lambda waypoints, *_: tuple(waypoints),
        ) as transpose:
            batch = prepare_transposition(
                input_files=(source,),
                alignments=(alignment,),
            )

        self.assertEqual(batch.failure_count, 0)
        waypoints, source_origin, target_origin, rotation = transpose.call_args.args
        self.assertEqual(source_origin, (51.2, -0.7))
        self.assertEqual(target_origin, (52.0, 0.25))
        self.assertEqual(rotation, 35.0)
        self.assertEqual(tuple(point[2] for point in waypoints), (0.0, 0.0))
        self.assertEqual(
            batch.prepared[0].trace.anchor,
            KmlCoordinate(longitude=0.25, latitude=52.0, altitude_m=0.0),
        )

    def test_manual_absolute_input_requires_ground_and_outputs_relative_height(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "absolute.kml"
            source.write_text(
                """<?xml version="1.0"?><kml><Placemark><LineString>
<altitudeMode>absolute</altitudeMode>
<coordinates>-1,51,100 -0.99,51.01,125</coordinates>
</LineString></Placemark></kml>""",
                encoding="utf-8",
            )
            missing = prepare_transposition(
                input_files=(source,),
                alignments=(ManualTranspositionAlignment(52.0, 0.0, 0.0),),
            )
            self.assertEqual(missing.failure_count, 1)
            self.assertIn("ground-reference elevation", missing.failed_items[0].message)

            with patch(
                "services.transpose_coordinates.transpose_wgs84_enu_points",
                side_effect=lambda waypoints, *_: tuple(waypoints),
            ):
                prepared = prepare_transposition(
                    input_files=(source,),
                    alignments=(
                        ManualTranspositionAlignment(52.0, 0.0, 0.0, 70.0),
                    ),
                )

        geometry = prepared.prepared[0].document.placemarks[0].geometry
        self.assertEqual(
            tuple(point.altitude_m for point in geometry.coordinates),
            (30.0, 55.0),
        )

    def test_mixed_output_plan_uses_runway_and_manual_default_names(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            plan = create_transposition_plan(
                [
                    self.fixture("gx_track.kml"),
                    self.fixture("line_string_namespaced.kml"),
                ],
                temp_dir,
                target_airfields=("Fairford", None),
            )

        self.assertEqual(plan.jobs[0].output_path.name, "gx-track-at-fairford.kml")
        self.assertEqual(
            plan.jobs[1].output_path.name,
            "line-string-namespaced-transposed.kml",
        )


if __name__ == "__main__":
    unittest.main()
