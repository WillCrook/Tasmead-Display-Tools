import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "kml"

from pages.debris_page import DebrisPage
from PyQt6.QtWidgets import QApplication, QFileDialog, QMessageBox
from file_dialog_state import FileDialogDirection, FileDialogWorkflow
from services import (
    KmlCoordinateError,
    KmlPoint,
    KmlStructureError,
    KmlTrack,
    KmlXmlError,
    RunwayReference,
    apply_source_runways,
    load_last_two_points_from_kml,
    parse_kml,
    parse_kml_track,
)
from services.transpose_coordinates import (
    TranspositionErrorCode,
    create_transposition_plan,
    customize_transposition_plan,
    run_transposition,
    write_kml,
)


class KmlParserTests(unittest.TestCase):
    def fixture(self, name):
        return FIXTURES / name

    def test_namespaced_nested_line_string_ignores_non_path_coordinates(self):
        track = parse_kml_track(self.fixture("line_string_namespaced.kml"))

        self.assertEqual(track.geometry_kind, "line_string")
        self.assertEqual(track.placemark_name, "Display route")
        self.assertEqual(
            track.points,
            (
                KmlPoint(latitude=51.2, longitude=-0.7, altitude_m=100.0),
                KmlPoint(latitude=51.3, longitude=-0.6, altitude_m=125.0),
            ),
        )

    def test_namespace_free_2d_line_string_preserves_missing_altitude(self):
        track = parse_kml_track(self.fixture("line_string_namespace_free_2d.kml"))

        self.assertEqual(track.placemark_name, "Two dimensional route")
        self.assertEqual([point.altitude_m for point in track.points], [None, None])

    def test_namespace_prefix_is_irrelevant_and_mixed_altitude_is_preserved(self):
        track = parse_kml_track(self.fixture("line_string_prefixed_mixed.kml"))

        self.assertEqual(track.geometry_kind, "line_string")
        self.assertEqual([point.altitude_m for point in track.points], [None, 125.0])

    def test_gx_prefix_is_irrelevant_and_track_order_is_preserved(self):
        track = parse_kml_track(self.fixture("gx_track.kml"))

        self.assertEqual(track.geometry_kind, "gx_track")
        self.assertEqual(track.placemark_name, "Recorded flight")
        self.assertEqual(
            [(point.latitude, point.longitude, point.altitude_m) for point in track.points],
            [(51.2, -0.7, 100.0), (51.3, -0.6, 125.0)],
        )
        self.assertEqual(
            [point.timestamp for point in track.points],
            ["2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z"],
        )
        self.assertEqual(track.altitude_mode, "clampToGround")

    def test_multiple_supported_geometries_are_not_concatenated(self):
        with self.assertRaises(KmlStructureError) as raised:
            parse_kml_track(self.fixture("multiple_paths.kml"))

        message = str(raised.exception)
        self.assertIn("exactly one is required", message)
        self.assertIn("First (LineString)", message)
        self.assertIn("Second (gx:Track)", message)

    def test_malformed_xml_reports_location(self):
        with self.assertRaises(KmlXmlError) as raised:
            parse_kml_track(self.fixture("malformed_xml.kml"))

        self.assertIn("line 1, column", str(raised.exception))

    def test_structure_errors_are_explicit(self):
        cases = [
            ("foreign_namespace.kml", "unsupported KML namespace"),
            ("no_path.kml", "no supported LineString or gx:Track"),
            ("one_point.kml", "at least two coordinates"),
        ]
        for filename, fragment in cases:
            with self.subTest(filename=filename):
                with self.assertRaises(KmlStructureError) as raised:
                    parse_kml_track(self.fixture(filename))
                self.assertIn(fragment, str(raised.exception))

    def test_coordinate_errors_are_never_silently_skipped(self):
        cases = [
            ("empty_coordinates.kml", "coordinates element is empty"),
            ("empty_gx_coord.kml", "coordinate 2"),
            ("wrong_arity.kml", "coordinate 1"),
            ("non_numeric.kml", "longitude value"),
            ("non_finite.kml", "not finite"),
            ("out_of_range.kml", "outside -90 to 90"),
        ]
        for filename, fragment in cases:
            with self.subTest(filename=filename):
                with self.assertRaises(KmlCoordinateError) as raised:
                    parse_kml_track(self.fixture(filename))
                message = str(raised.exception)
                self.assertIn(filename, message)
                self.assertIn(fragment, message)

    def test_kmz_is_rejected_without_attempting_archive_or_network_access(self):
        with self.assertRaises(KmlStructureError) as raised:
            parse_kml_track(FIXTURES / "not-present.kmz")
        self.assertIn("KMZ archives are not supported", str(raised.exception))

    def test_legacy_adapters_keep_tuple_shapes_and_zero_altitude(self):
        path = self.fixture("line_string_namespace_free_2d.kml")

        self.assertEqual(parse_kml(path), [(51.2, -0.7, 0.0), (51.3, -0.6, 0.0)])
        self.assertEqual(
            load_last_two_points_from_kml(path),
            (51.2, -0.7, 51.3, -0.6, 0.0),
        )


class TranspositionKmlTests(unittest.TestCase):
    def fixture(self, name):
        return FIXTURES / name

    def reviewed_plan(self, plan, elevation=0.0):
        runway = RunwayReference(51.2, -0.7, 32.0, elevation)
        return apply_source_runways(plan, (runway,) * len(plan.jobs))

    def run_plan(self, plan, elevation=0.0):
        plan = self.reviewed_plan(plan, elevation)
        return run_transposition(
            plan,
            target_runway=RunwayReference(51.0, -1.0, 90.0),
        )

    def test_both_supported_geometry_types_reach_geodesic_contract(self):
        cases = [
            ("line_string_namespaced.kml", ((51.2, -0.7, 0.0), (51.3, -0.6, 0.0))),
            ("gx_track.kml", ((51.2, -0.7, 0.0), (51.3, -0.6, 0.0))),
        ]
        for filename, expected in cases:
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as temp_dir:
                with (
                    patch(
                        "services.transpose_coordinates.transpose_geodesic_points",
                        side_effect=lambda waypoints, *_: tuple(waypoints),
                    ) as transpose,
                    patch("services.transpose_coordinates.write_kml") as write,
                ):
                    plan = create_transposition_plan(
                        [self.fixture(filename)],
                        temp_dir,
                        "RAF Fairford",
                    )
                    plan = self.reviewed_plan(plan)
                    result = run_transposition(
                        plan,
                        target_runway=RunwayReference(51.0, -1.0, 90.0),
                    )

                self.assertTrue(result.succeeded)
                self.assertEqual(
                    plan.jobs[0].output_path.name,
                    f"{Path(filename).stem.replace('_', '-')}-at-raf-fairford.kml",
                )
                self.assertEqual(transpose.call_args.args[0], list(expected))
                self.assertEqual(write.call_args.args[1], expected)

    def test_missing_clamped_altitude_outputs_zero_relative_height(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch(
                    "services.transpose_coordinates.transpose_geodesic_points",
                    side_effect=lambda waypoints, *_: tuple(waypoints),
                ) as transpose,
                patch("services.transpose_coordinates.write_kml") as write,
            ):
                plan = create_transposition_plan(
                    [self.fixture("line_string_namespace_free_2d.kml")],
                    temp_dir,
                    "Field",
                )
                plan = self.reviewed_plan(plan, elevation=125.0)
                result = run_transposition(
                    plan,
                    target_runway=RunwayReference(51.0, -1.0, 90.0),
                )

        self.assertTrue(result.succeeded)
        self.assertEqual(
            transpose.call_args.args[0],
            [(51.2, -0.7, 0.0), (51.3, -0.6, 0.0)],
        )
        self.assertEqual(
            write.call_args.args[1],
            ((51.2, -0.7, 0.0), (51.3, -0.6, 0.0)),
        )

    def test_missing_absolute_altitudes_are_omitted_warned_and_embedded(self):
        track = KmlTrack(
            points=(
                KmlPoint(51.0, -1.0, None),
                KmlPoint(51.1, -0.9, 100.0),
                KmlPoint(51.2, -0.8, None),
                KmlPoint(51.3, -0.7, 125.0),
                KmlPoint(51.4, -0.6, None),
            ),
            geometry_kind="line_string",
            placemark_name="Mixed absolute altitude",
            altitude_mode="absolute",
        )
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch(
                "services.transpose_coordinates.parse_kml_track",
                return_value=track,
            ),
            patch(
                "services.transpose_coordinates.transpose_geodesic_points",
                side_effect=lambda waypoints, *_: tuple(waypoints),
            ),
        ):
            plan = create_transposition_plan(
                [self.fixture("line_string_namespaced.kml")],
                temp_dir,
                "Field",
            )
            result = self.run_plan(plan, elevation=80.0)

            self.assertTrue(result.succeeded)
            self.assertEqual(
                result.successful[0].warnings,
                (
                    "Omitted 3 source coordinate(s) because absolute altitude was missing.",
                ),
            )
            root = ET.parse(result.successful[0].output_path).getroot()

        namespace = {"kml": "http://www.opengis.net/kml/2.2"}
        placemark = root.find("kml:Document/kml:Placemark", namespace)
        self.assertEqual(
            placemark.find("kml:description", namespace).text,
            "Processing warnings:\n"
            "- Omitted 3 source coordinate(s) because absolute altitude was missing.",
        )
        self.assertEqual(
            placemark.find("kml:LineString/kml:coordinates", namespace).text.splitlines(),
            ["-0.9000000,51.1000000,20.000", "-0.7000000,51.3000000,45.000"],
        )

    def test_too_few_absolute_altitudes_fail_without_writing(self):
        track = KmlTrack(
            points=(
                KmlPoint(51.0, -1.0, None),
                KmlPoint(51.1, -0.9, 100.0),
                KmlPoint(51.2, -0.8, None),
            ),
            geometry_kind="line_string",
            placemark_name="Insufficient absolute altitude",
            altitude_mode="absolute",
        )
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch(
                "services.transpose_coordinates.parse_kml_track",
                return_value=track,
            ),
            patch("services.transpose_coordinates.write_kml") as write,
        ):
            plan = create_transposition_plan(
                [self.fixture("line_string_namespaced.kml")],
                temp_dir,
                "Field",
            )
            result = self.run_plan(plan, elevation=80.0)

        self.assertTrue(result.failed)
        self.assertEqual(
            result.failed_outcomes[0].error.code,
            TranspositionErrorCode.TRANSFORMATION,
        )
        self.assertIn("Omitted 2 source coordinate(s)", result.failed_outcomes[0].error.message)
        self.assertIn("fewer than two valid coordinates remain", result.failed_outcomes[0].error.message)
        write.assert_not_called()

    def test_parse_error_is_a_failed_outcome_without_rotation_or_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch("services.transpose_coordinates.transpose_geodesic_points") as transpose,
                patch("services.transpose_coordinates.write_kml") as write,
            ):
                plan = create_transposition_plan(
                    [self.fixture("wrong_arity.kml")],
                    temp_dir,
                    "Field",
                )
                result = self.run_plan(plan)

        self.assertTrue(result.failed)
        self.assertEqual(result.failure_count, 1)
        self.assertEqual(result.failed_outcomes[0].error.code, TranspositionErrorCode.INPUT_KML)
        self.assertIn("must contain longitude,latitude", result.failed_outcomes[0].error.message)
        transpose.assert_not_called()
        write.assert_not_called()

    def test_programmatic_run_without_reviewed_source_alignment_fails_clearly(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            plan = create_transposition_plan(
                [self.fixture("line_string_namespaced.kml")],
                temp_dir,
                "Field",
            )
            result = run_transposition(
                plan,
                target_runway=RunwayReference(51.0, -1.0, 90.0),
            )

        self.assertTrue(result.failed)
        self.assertEqual(
            result.failed_outcomes[0].error.code,
            TranspositionErrorCode.TRANSFORMATION,
        )
        self.assertIn("has not been reviewed", result.failed_outcomes[0].error.message)

    def test_naming_normalizes_components_fallbacks_extensions_and_length(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            plan = create_transposition_plan(
                [
                    Path(temp_dir) / "Red Arrows.Display.KML",
                    Path(temp_dir) / "Café & Jet.kml",
                    Path(temp_dir) / "東京.kml",
                    Path(temp_dir) / ("A" * 180 + ".kml"),
                ],
                temp_dir,
                "RAF Fairford!",
            )
            blank_target = create_transposition_plan(
                [Path(temp_dir) / "---.kml"],
                temp_dir,
                "東京",
            )

        names = [job.output_path.name for job in plan.jobs]
        self.assertEqual(names[0], "red-arrows-display-at-raf-fairford.kml")
        self.assertEqual(names[1], "cafe-jet-at-raf-fairford.kml")
        self.assertEqual(names[2], "aircraft-at-raf-fairford.kml")
        self.assertLessEqual(len(Path(names[3]).stem), 200)
        self.assertEqual(
            blank_target.jobs[0].output_path.name,
            "aircraft-at-airfield.kml",
        )

    def test_existing_and_within_batch_collisions_are_numbered_case_insensitively(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "DISPLAY-AT-FIELD.KML").write_text("existing", encoding="utf-8")
            plan = create_transposition_plan(
                [root / "one" / "Display.kml", root / "two" / "Display.kml"],
                root,
                "Field",
            )

        self.assertEqual(
            [job.output_path.name for job in plan.jobs],
            ["display-at-field-2.kml", "display-at-field-3.kml"],
        )

    def test_custom_output_names_are_normalized_and_validated(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plan = create_transposition_plan(
                [
                    self.fixture("line_string_namespaced.kml"),
                    self.fixture("gx_track.kml"),
                ],
                root,
                "Field",
            )
            customized = customize_transposition_plan(
                plan,
                ("Display route.final", "Second.KML"),
            )

            self.assertEqual(
                [job.output_path.name for job in customized.jobs],
                ["Display route.final.kml", "Second.KML"],
            )
            for invalid in (
                ("", "second.kml"),
                ("folder/name.kml", "second.kml"),
                ("same.kml", "SAME.KML"),
            ):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    customize_transposition_plan(plan, invalid)

    def test_runtime_collision_fails_without_overwriting_or_renaming(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plan = create_transposition_plan(
                [self.fixture("line_string_namespaced.kml")],
                root,
                "Field",
            )
            plan = customize_transposition_plan(plan, ("chosen-name.kml",))
            plan.jobs[0].output_path.write_text("appeared later", encoding="utf-8")

            with patch(
                "services.transpose_coordinates.transpose_geodesic_points",
                side_effect=lambda waypoints, *_: tuple(waypoints),
            ):
                result = self.run_plan(plan)

            self.assertTrue(result.failed)
            self.assertEqual(
                result.failed_outcomes[0].error.code,
                TranspositionErrorCode.OUTPUT_COLLISION,
            )
            self.assertEqual(
                plan.jobs[0].output_path.read_text(encoding="utf-8"),
                "appeared later",
            )
            self.assertFalse((root / "chosen-name-2.kml").exists())

    def test_approved_existing_output_is_atomically_replaced_at_exact_name(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plan = create_transposition_plan(
                [self.fixture("line_string_namespaced.kml")],
                root,
                "Field",
            )
            destination = root / "chosen-name.kml"
            destination.write_text("replace me", encoding="utf-8")
            plan = customize_transposition_plan(
                plan,
                (destination.name,),
                approved_overwrites=(destination,),
            )

            with patch(
                "services.transpose_coordinates.transpose_geodesic_points",
                side_effect=lambda waypoints, *_: tuple(waypoints),
            ):
                result = self.run_plan(plan)

            self.assertTrue(result.succeeded)
            self.assertEqual(result.successful[0].output_path, destination)
            self.assertEqual(len(parse_kml_track(destination).points), 2)

    def test_multiple_inputs_write_multiple_distinct_parseable_outputs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            plan = create_transposition_plan(
                [
                    self.fixture("line_string_namespaced.kml"),
                    self.fixture("gx_track.kml"),
                ],
                temp_dir,
                "RAF Fairford",
            )
            with patch(
                "services.transpose_coordinates.transpose_geodesic_points",
                side_effect=lambda waypoints, *_: tuple(waypoints),
            ):
                result = self.run_plan(plan)

            self.assertTrue(result.succeeded)
            self.assertEqual(
                [output.output_path.name for output in result.successful],
                [
                    "line-string-namespaced-at-raf-fairford.kml",
                    "gx-track-at-raf-fairford.kml",
                ],
            )
            for output in result.successful:
                self.assertTrue(output.output_path.is_file())
                self.assertEqual(len(parse_kml_track(output.output_path).points), 2)

    def test_failures_do_not_stop_later_jobs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            plan = create_transposition_plan(
                [
                    self.fixture("line_string_namespaced.kml"),
                    self.fixture("wrong_arity.kml"),
                    self.fixture("gx_track.kml"),
                ],
                temp_dir,
                "Field",
            )
            with patch(
                "services.transpose_coordinates.transpose_geodesic_points",
                side_effect=lambda waypoints, *_: tuple(waypoints),
            ):
                result = self.run_plan(plan)

            self.assertTrue(result.partially_succeeded)
            self.assertEqual(result.success_count, 2)
            self.assertEqual(result.failure_count, 1)
            self.assertTrue(result.successful[0].output_path.is_file())
            self.assertTrue(result.successful[1].output_path.is_file())
            self.assertEqual(result.failed_outcomes[0].input_path.name, "wrong_arity.kml")
            self.assertFalse(result.failed_outcomes[0].planned_output_path.exists())
            self.assertEqual(
                [outcome.input_path.name for outcome in result.outcomes],
                ["line_string_namespaced.kml", "wrong_arity.kml", "gx_track.kml"],
            )
            self.assertTrue(plan.jobs[2].output_path.exists())

    def test_all_failed_batch_never_reports_success(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            plan = create_transposition_plan(
                [self.fixture("wrong_arity.kml"), self.fixture("one_point.kml")],
                temp_dir,
                "Field",
            )
            result = self.run_plan(plan)

        self.assertTrue(result.failed)
        self.assertFalse(result.succeeded)
        self.assertFalse(result.partially_succeeded)
        self.assertEqual(result.success_count, 0)
        self.assertEqual(result.failure_count, 2)

    def test_write_failure_is_recorded_and_later_jobs_still_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            plan = create_transposition_plan(
                [
                    self.fixture("line_string_namespaced.kml"),
                    self.fixture("gx_track.kml"),
                ],
                temp_dir,
                "Field",
            )

            def fail_first_write(path, *args, **kwargs):
                if Path(path) == plan.jobs[0].output_path:
                    raise OSError("disk full")
                return write_kml(path, *args, **kwargs)

            with (
                patch(
                    "services.transpose_coordinates.transpose_geodesic_points",
                    side_effect=lambda waypoints, *_: tuple(waypoints),
                ),
                patch(
                    "services.transpose_coordinates.write_kml",
                    side_effect=fail_first_write,
                ),
            ):
                result = self.run_plan(plan)

            self.assertTrue(result.partially_succeeded)
            self.assertEqual(result.failed_outcomes[0].error.code, TranspositionErrorCode.FILESYSTEM_WRITE)
            self.assertTrue(result.successful[0].output_path.is_file())

    def test_unexpected_error_is_safe_for_ui_and_keeps_diagnostic_type(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            plan = create_transposition_plan(
                [self.fixture("line_string_namespaced.kml")], temp_dir, "Field"
            )
            with patch(
                "services.transpose_coordinates.parse_kml_track",
                side_effect=RuntimeError("internal details"),
            ):
                result = self.run_plan(plan)

        error = result.failed_outcomes[0].error
        self.assertEqual(error.exception_type, "RuntimeError")
        self.assertEqual(error.message, "An unexpected error occurred while processing this file.")

    def test_legacy_single_input_returns_result_and_multiple_inputs_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "legacy.kml"
            with patch(
                "services.transpose_coordinates.transpose_geodesic_points",
                side_effect=lambda waypoints, *_: tuple(waypoints),
            ), self.assertWarns(DeprecationWarning):
                result = run_transposition(
                    [self.fixture("line_string_namespaced.kml")],
                    output,
                    51.0,
                    -1.0,
                    90.0,
                    source_runway=RunwayReference(51.2, -0.7, 32.0, 0.0),
                )
            self.assertTrue(result.succeeded)
            self.assertTrue(output.is_file())

            with self.assertRaisesRegex(ValueError, "exactly one"):
                run_transposition(
                    [self.fixture("line_string_namespaced.kml"), self.fixture("gx_track.kml")],
                    output,
                    51.0,
                    -1.0,
                    90.0,
                    source_runway=RunwayReference(51.2, -0.7, 32.0, 0.0),
                )

    def test_failed_write_removes_the_incomplete_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "output.kml"
            with (
                patch(
                    "services.kml_export.render_kml",
                    side_effect=OSError("render failed"),
                ),
                self.assertRaisesRegex(OSError, "render failed"),
            ):
                write_kml(output_path, [(1.0, 2.0, 3.0)], "Aircraft")

            self.assertFalse(output_path.exists())

    def test_kml_document_name_is_xml_escaped_and_utf8_parseable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "output.kml"
            write_kml(
                output_path,
                [(1.0, 2.0, 3.0), (1.1, 2.1, 3.1)],
                "A&B <Display>",
            )

            root = ET.parse(output_path).getroot()
            namespace = {"kml": "http://www.opengis.net/kml/2.2"}
            self.assertEqual(
                root.find("kml:Document/kml:name", namespace).text,
                "A&B <Display> Adjusted Coordinates",
            )
            line = root.find("kml:Document/kml:Placemark/kml:LineString", namespace)
            self.assertEqual(
                root.find("kml:Document/kml:Style/kml:LineStyle/kml:color", namespace).text,
                "aaff00ff",
            )
            self.assertEqual(
                root.find("kml:Document/kml:Style/kml:LineStyle/kml:width", namespace).text,
                "6",
            )
            self.assertEqual(line.find("kml:extrude", namespace).text, "1")
            self.assertEqual(line.find("kml:tessellate", namespace).text, "0")
            self.assertEqual(line.find("kml:altitudeMode", namespace).text, "relativeToGround")


class DebrisKmlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.app_patch = patch(
            "pages.debris_page.app_data_path",
            side_effect=lambda relative: str(root / "app-data" / relative),
        )
        self.resource_patch = patch(
            "pages.debris_page.resource_path",
            side_effect=lambda relative: str(root / "bundle" / relative),
        )
        self.app_patch.start()
        self.resource_patch.start()
        self.remember_patch = patch("pages.debris_page.remember_file_selection")
        self.remember_selection = self.remember_patch.start()
        self.page = DebrisPage()

    def tearDown(self):
        self.page.close()
        self.remember_patch.stop()
        self.resource_patch.stop()
        self.app_patch.stop()
        self.temp_dir.cleanup()

    def fixture(self, name):
        return FIXTURES / name

    def test_browse_automatically_parses_both_geometry_types(self):
        for filename in ("line_string_namespaced.kml", "gx_track.kml"):
            with self.subTest(filename=filename):
                path = str(self.fixture(filename))
                with patch.object(QFileDialog, "getOpenFileName", return_value=(path, "")):
                    self.page.browse_file(None)

                self.assertTrue(self.page._kml_state.ready)
                self.assertEqual(
                    self.page._kml_state.coordinates,
                    (51.2, -0.7, 51.3, -0.6),
                )
                self.assertEqual(self.page.alt_m.text(), "125.0")
                self.assertEqual(self.page.kml_status_label.text(), "KML ready.")
                self.assertEqual(self.page.load_kml_btn.text(), "Reload selected KML")
                self.assertTrue(self.page.load_kml_btn.isEnabled())

    def test_browse_uses_and_remembers_debris_input_directory(self):
        path = str(self.fixture("line_string_namespaced.kml"))
        initial = str(Path(self.temp_dir.name) / "remembered-input")
        self.remember_selection.reset_mock()
        with (
            patch(
                "pages.debris_page.remembered_directory",
                return_value=initial,
            ) as remembered,
            patch.object(
                QFileDialog,
                "getOpenFileName",
                return_value=(path, ""),
            ) as file_dialog,
        ):
            self.page.browse_file(None)

        remembered.assert_called_once_with(
            FileDialogWorkflow.DEBRIS,
            FileDialogDirection.INPUT,
        )
        file_dialog.assert_called_once_with(
            self.page,
            "Open KML",
            initial,
            "KML Files (*.kml)",
        )
        self.remember_selection.assert_called_once_with(
            FileDialogWorkflow.DEBRIS,
            FileDialogDirection.INPUT,
            path,
        )

    def test_missing_final_altitude_clears_stale_value_and_warns_for_manual_entry(self):
        self.page.alt_m.setText("999")
        path = self.fixture("line_string_namespace_free_2d.kml")
        with patch.object(QMessageBox, "warning") as warning:
            ready = self.page.select_and_parse_kml(path)

        self.assertTrue(ready)
        self.assertEqual(self.page._kml_state.coordinates, (51.2, -0.7, 51.3, -0.6))
        self.assertEqual(self.page.alt_m.text(), "")
        self.assertIn("enter altitude", self.page.kml_status_label.text())
        self.assertIn("has no altitude", warning.call_args.args[2])

    def test_parser_failure_invalidates_previous_extraction_and_shows_error(self):
        self.page.select_and_parse_kml(self.fixture("line_string_namespaced.kml"))
        invalid = str(self.fixture("wrong_arity.kml"))
        with patch.object(QMessageBox, "critical") as critical:
            ready = self.page.select_and_parse_kml(invalid)

        self.assertFalse(ready)
        self.assertEqual(self.page.kml_input_path, invalid)
        self.assertIsNone(self.page._kml_state.coordinates)
        self.assertIsNotNone(self.page._kml_state.error)
        self.assertEqual(self.page.kml_meta_pen_lat.text(), "Penultimate latitude: —")
        self.assertEqual(self.page.alt_m.text(), "")
        self.assertIn("coordinate 1", critical.call_args.args[2])

    def test_browse_cancel_preserves_ready_selection(self):
        self.page.select_and_parse_kml(self.fixture("line_string_namespaced.kml"))
        original = self.page._kml_state
        with patch.object(QFileDialog, "getOpenFileName", return_value=("", "")):
            self.page.browse_file(None)

        self.assertIs(self.page._kml_state, original)

    def test_drop_uses_the_same_automatic_parse_flow(self):
        path = str(self.fixture("gx_track.kml"))
        url = SimpleNamespace(toLocalFile=lambda: path)
        mime_data = SimpleNamespace(urls=lambda: [url])
        event = SimpleNamespace(mimeData=lambda: mime_data)

        self.page.drop_event(event)

        self.assertEqual(self.page.kml_input_path, path)
        self.assertTrue(self.page._kml_state.ready)
        self.assertEqual(self.page.alt_m.text(), "125.0")

    def test_reselecting_same_path_reparses_instead_of_reusing_coordinates(self):
        path = str(self.fixture("line_string_namespaced.kml"))
        first = KmlTrack(
            points=(KmlPoint(1.0, 2.0, 3.0), KmlPoint(4.0, 5.0, 6.0)),
            geometry_kind="line_string",
            placemark_name=None,
        )
        second = KmlTrack(
            points=(KmlPoint(7.0, 8.0, 9.0), KmlPoint(10.0, 11.0, 12.0)),
            geometry_kind="line_string",
            placemark_name=None,
        )
        with patch("pages.debris_page.parse_kml_track", side_effect=[first, second]) as parser:
            self.page.select_and_parse_kml(path)
            self.page.select_and_parse_kml(path)

        self.assertEqual(parser.call_count, 2)
        self.assertEqual(self.page._kml_state.coordinates, (7.0, 8.0, 10.0, 11.0))
        self.assertEqual(self.page.alt_m.text(), "12.0")

    def test_reload_preserves_manual_altitude_for_two_dimensional_kml(self):
        path = self.fixture("line_string_namespace_free_2d.kml")
        with patch.object(QMessageBox, "warning"):
            self.page.select_and_parse_kml(path)
        self.page.alt_m.setText("350")

        with patch.object(QMessageBox, "warning") as warning:
            ready = self.page.reload_selected_kml()

        self.assertTrue(ready)
        self.assertEqual(self.page.alt_m.text(), "350")
        self.assertIn("using entered altitude", self.page.kml_status_label.text())
        warning.assert_not_called()

    def test_missing_malformed_and_unsupported_files_are_retryable_errors(self):
        cases = [
            FIXTURES / "not-present.kml",
            self.fixture("malformed_xml.kml"),
            self.fixture("no_path.kml"),
        ]
        for path in cases:
            with self.subTest(path=path):
                ready = self.page.select_and_parse_kml(path, notify=False)
                self.assertFalse(ready)
                self.assertEqual(self.page.kml_input_path, str(path))
                self.assertIsNone(self.page._kml_state.coordinates)
                self.assertTrue(self.page._kml_state.error)
                self.assertTrue(self.page.load_kml_btn.isEnabled())

    def test_invalid_selection_blocks_before_output_picker_and_calculator(self):
        self.page.select_and_parse_kml(self.fixture("wrong_arity.kml"), notify=False)
        with (
            patch.object(QMessageBox, "warning") as warning,
            patch.object(QFileDialog, "getSaveFileName") as save_dialog,
            patch.object(self.page, "run_debris_calculator") as calculator,
        ):
            self.page.run_simulation()

        self.assertIn("could not be loaded", warning.call_args.args[2])
        save_dialog.assert_not_called()
        calculator.assert_not_called()

    def test_run_parses_legacy_path_without_manual_extraction(self):
        path = str(self.fixture("line_string_namespaced.kml"))
        self.page.kml_input_path = path
        self.page.alt_m.setText("999")
        self.page.terrain_m.setText("0")
        output = str(Path(self.temp_dir.name) / "debris.kml")
        with (
            patch.object(QFileDialog, "getSaveFileName", return_value=(output, "")),
            patch.object(self.page, "run_debris_calculator") as calculator,
        ):
            self.page.run_simulation()

        self.assertTrue(self.page._kml_state.ready)
        self.assertEqual(self.page.alt_m.text(), "125.0")
        self.assertEqual(
            calculator.call_args.kwargs["input_coords_hook"],
            (51.2, -0.7, 51.3, -0.6),
        )

    def test_combined_coordinate_and_track_modes_pass_decimal_values(self):
        output = str(Path(self.temp_dir.name) / "debris.kml")
        self.page.alt_m.setText("100")
        self.page.terrain_m.setText("0")

        cases = (
            (
                self.page.rb_coords,
                lambda: (
                    self.page.coordinate1_input.setText("51.2 -0.7"),
                    self.page.coordinate2_input.setText("51.3/-0.6"),
                ),
                "input_coords_hook",
                (51.2, -0.7, 51.3, -0.6),
            ),
            (
                self.page.rb_bearing,
                lambda: (
                    self.page.bearing_coordinate_input.setText(
                        '51°12\'0"N, 0°42\'0"W'
                    ),
                    self.page.azimuth_input.setText("135"),
                ),
                "input_bearing_hook",
                (51.2, -0.7, 135.0),
            ),
        )

        for radio_button, configure, argument, expected in cases:
            with self.subTest(argument=argument):
                radio_button.setChecked(True)
                configure()
                with (
                    patch.object(
                        QFileDialog,
                        "getSaveFileName",
                        return_value=(output, ""),
                    ),
                    patch.object(self.page, "run_debris_calculator") as calculator,
                ):
                    self.page.run_simulation()

                self.assertEqual(calculator.call_args.kwargs[argument], expected)

    def test_invalid_combined_coordinate_blocks_before_output_picker(self):
        self.page.rb_coords.setChecked(True)
        self.page.alt_m.setText("100")
        self.page.terrain_m.setText("0")
        self.page.coordinate1_input.setText("invalid")
        self.page.coordinate2_input.setText("51.3, -0.6")

        with (
            patch.object(QMessageBox, "warning") as warning,
            patch.object(QFileDialog, "getSaveFileName") as save_dialog,
            patch.object(self.page, "run_debris_calculator") as calculator,
        ):
            self.page.run_simulation()

        self.assertIn("Coordinate 1", warning.call_args.args[2])
        save_dialog.assert_not_called()
        calculator.assert_not_called()

    def test_output_picker_uses_remembered_folder_and_keeps_custom_filename(self):
        self.page.select_and_parse_kml(
            self.fixture("line_string_namespaced.kml"),
            notify=False,
        )
        self.page.terrain_m.setText("0")
        initial = str(Path(self.temp_dir.name) / "debris_trajectory.kml")
        selected = str(Path(self.temp_dir.name) / "My custom result")
        self.remember_selection.reset_mock()
        with (
            patch(
                "pages.debris_page.suggested_save_path",
                return_value=initial,
            ) as suggestion,
            patch.object(
                QFileDialog,
                "getSaveFileName",
                return_value=(selected, ""),
            ) as save_dialog,
            patch.object(self.page, "run_debris_calculator") as calculator,
        ):
            self.page.run_simulation()

        suggestion.assert_called_once_with(
            FileDialogWorkflow.DEBRIS,
            "debris_trajectory.kml",
        )
        save_dialog.assert_called_once_with(
            self.page,
            "Save output KML",
            initial,
            "KML Files (*.kml)",
        )
        expected = f"{selected}.kml"
        self.remember_selection.assert_called_once_with(
            FileDialogWorkflow.DEBRIS,
            FileDialogDirection.OUTPUT,
            expected,
        )
        self.assertEqual(calculator.call_args.kwargs["output_kml"], expected)


if __name__ == "__main__":
    unittest.main()
