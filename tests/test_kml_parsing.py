import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import MagicMock, patch


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "kml"

from pages.debris_page import DebrisPage
from services import (
    KmlCoordinateError,
    KmlPoint,
    KmlStructureError,
    KmlTrack,
    KmlXmlError,
    load_last_two_points_from_kml,
    parse_kml,
    parse_kml_track,
)
from services.transpose_coordinates import run_transposition


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

    def test_both_supported_geometry_types_reach_existing_rotation_contract(self):
        cases = [
            ("line_string_namespaced.kml", [(51.2, -0.7, 100.0), (51.3, -0.6, 125.0)]),
            ("gx_track.kml", [(51.2, -0.7, 100.0), (51.3, -0.6, 125.0)]),
        ]
        for filename, expected in cases:
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as temp_dir:
                with (
                    patch(
                        "services.transpose_coordinates.rotate_route",
                        side_effect=lambda waypoints, *_: waypoints,
                    ) as rotate,
                    patch("services.transpose_coordinates.write_kml") as write,
                ):
                    run_transposition(
                        [str(self.fixture(filename))],
                        str(Path(temp_dir) / "output.kml"),
                        51.0,
                        -1.0,
                        90.0,
                    )

                self.assertEqual(rotate.call_args.args[0], expected)
                self.assertEqual(write.call_args.args[1], expected)

    def test_missing_altitude_uses_source_elevation_then_outputs_zero_relative_height(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch(
                    "services.transpose_coordinates.rotate_route",
                    side_effect=lambda waypoints, *_: waypoints,
                ) as rotate,
                patch("services.transpose_coordinates.write_kml") as write,
            ):
                run_transposition(
                    [str(self.fixture("line_string_namespace_free_2d.kml"))],
                    str(Path(temp_dir) / "output.kml"),
                    51.0,
                    -1.0,
                    90.0,
                    ground_reference_elevation=125.0,
                )

        self.assertEqual(
            rotate.call_args.args[0],
            [(51.2, -0.7, 125.0), (51.3, -0.6, 125.0)],
        )
        self.assertEqual(
            write.call_args.args[1],
            [(51.2, -0.7, 0.0), (51.3, -0.6, 0.0)],
        )

    def test_parse_error_propagates_before_rotation_or_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch("services.transpose_coordinates.rotate_route") as rotate,
                patch("services.transpose_coordinates.write_kml") as write,
                self.assertRaises(KmlCoordinateError),
            ):
                run_transposition(
                    [str(self.fixture("wrong_arity.kml"))],
                    str(Path(temp_dir) / "output.kml"),
                    51.0,
                    -1.0,
                    90.0,
                )

        rotate.assert_not_called()
        write.assert_not_called()


class DebrisKmlTests(unittest.TestCase):
    def fixture(self, name):
        return FIXTURES / name

    def page_stub(self, filename):
        stub = SimpleNamespace(
            kml_input_path=str(self.fixture(filename)),
            kml_values=(1.0, 2.0, 3.0, 4.0),
            kml_meta_pen_lat=MagicMock(),
            kml_meta_pen_lon=MagicMock(),
            kml_meta_fin_lat=MagicMock(),
            kml_meta_fin_lon=MagicMock(),
            alt_m=MagicMock(),
            file_label=MagicMock(),
        )
        stub.clear_kml_metadata = MethodType(DebrisPage.clear_kml_metadata, stub)
        return stub

    def test_both_geometry_types_populate_last_two_points_and_altitude(self):
        for filename in ("line_string_namespaced.kml", "gx_track.kml"):
            with self.subTest(filename=filename):
                stub = self.page_stub(filename)
                with patch("pages.debris_page.QMessageBox.warning") as warning:
                    DebrisPage.load_kml_metadata(stub)

                self.assertEqual(stub.kml_values, (51.2, -0.7, 51.3, -0.6))
                stub.alt_m.setText.assert_called_once_with("125.0")
                warning.assert_not_called()

    def test_missing_final_altitude_clears_stale_value_and_warns_for_manual_entry(self):
        stub = self.page_stub("line_string_namespace_free_2d.kml")
        with patch("pages.debris_page.QMessageBox.warning") as warning:
            DebrisPage.load_kml_metadata(stub)

        self.assertEqual(stub.kml_values, (51.2, -0.7, 51.3, -0.6))
        stub.alt_m.clear.assert_called_once_with()
        self.assertIn("has no altitude", warning.call_args.args[2])

    def test_parser_failure_invalidates_previous_extraction_and_shows_error(self):
        stub = self.page_stub("wrong_arity.kml")
        with patch("pages.debris_page.QMessageBox.critical") as critical:
            DebrisPage.load_kml_metadata(stub)

        self.assertIsNone(stub.kml_values)
        self.assertIn("coordinate 1", critical.call_args.args[2])
        stub.kml_meta_pen_lat.setText.assert_called_with("Penultimate latitude: —")

    def test_selecting_another_file_invalidates_previous_extraction(self):
        stub = self.page_stub("gx_track.kml")
        replacement = str(self.fixture("line_string_namespaced.kml"))
        with patch("pages.debris_page.QFileDialog.getOpenFileName", return_value=(replacement, "")):
            DebrisPage.browse_file(stub, None)

        self.assertEqual(stub.kml_input_path, replacement)
        self.assertIsNone(stub.kml_values)

    def test_empty_altitude_blocks_simulation_before_other_inputs_are_used(self):
        stub = SimpleNamespace(alt_m=MagicMock())
        stub.alt_m.text.return_value = ""
        with patch("pages.debris_page.QMessageBox.warning") as warning:
            DebrisPage.run_simulation(stub)

        self.assertIn("valid altitude", warning.call_args.args[2])


if __name__ == "__main__":
    unittest.main()
