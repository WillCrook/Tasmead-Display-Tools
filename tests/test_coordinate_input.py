import os
import sys
import unittest
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from PyQt6.QtWidgets import QApplication

from pages.coordinate_input import CoordinatePairInput
from services import (
    CoordinateInputError,
    CoordinatePair,
    format_coordinate_pair,
    parse_coordinate_pair,
)


class CoordinateParserTests(unittest.TestCase):
    def test_decimal_pairs_accept_space_comma_and_slash(self):
        for value in (
            "51.272833 -0.792044",
            "51.272833, -0.792044",
            "51.272833/-0.792044",
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    parse_coordinate_pair(value),
                    CoordinatePair(51.272833, -0.792044),
                )

    def test_dms_accepts_ascii_unicode_spaced_and_lowercase_variants(self):
        cases = (
            '51°16\'22.2"N, 0°47\'31.4"W',
            "51º16′22.2″N / 0º47′31.4″W",
            "51 16 22.2 N 0 47 31.4 W",
            "51 16 22.2 n / 0 47 31.4 w",
            "N 51 16 22.2 / W 0 47 31.4",
        )
        for value in cases:
            with self.subTest(value=value):
                pair = parse_coordinate_pair(value)
                self.assertAlmostEqual(pair.latitude, 51.27283333333333)
                self.assertAlmostEqual(pair.longitude, -0.7920555555555555)

    def test_decimal_hemispheres_and_mixed_components_are_supported(self):
        pair = parse_coordinate_pair('51.5N / 0°30\'0"W')
        self.assertEqual(pair, CoordinatePair(51.5, -0.5))

    def test_southern_western_boundaries_and_negative_zero(self):
        self.assertEqual(
            parse_coordinate_pair("90S, 180W"),
            CoordinatePair(-90.0, -180.0),
        )
        self.assertEqual(
            format_coordinate_pair(parse_coordinate_pair("-0 -0")),
            "0, 0",
        )

    def test_invalid_coordinate_shapes_and_values_are_rejected(self):
        cases = (
            "",
            "51",
            "51, -1, 20",
            "51; -1",
            "NaN, -1",
            "1e309, -1",
            "91, -1",
            "51, 181",
            '51°60\'0"N, 1°0\'0"W',
            '51°0\'60"N, 1°0\'0"W',
            '51°0\'0"E, 1°0\'0"N',
            '-51N, 1W',
            '+51S, 1W',
            "51 30, 1",
        )
        for value in cases:
            with self.subTest(value=value), self.assertRaises(CoordinateInputError):
                parse_coordinate_pair(value)


class CoordinatePairInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_editing_finished_normalizes_valid_input(self):
        field = CoordinatePairInput("Coordinates")
        field.setText('51°16\'22.2"N, 0°47\'31.4"W')

        field.editingFinished.emit()

        self.assertEqual(field.text(), "51.27283333, -0.79205556")

    def test_editing_finished_leaves_invalid_input_visible(self):
        field = CoordinatePairInput("Coordinates")
        field.setText("not a coordinate")

        field.editingFinished.emit()

        self.assertEqual(field.text(), "not a coordinate")
        with self.assertRaisesRegex(CoordinateInputError, "Coordinates"):
            field.coordinates()

    def test_preset_components_round_trip_existing_separate_keys(self):
        field = CoordinatePairInput("Coordinates")
        field.set_components("51.272833", "-0.792044")

        self.assertEqual(field.text(), "51.272833, -0.792044")
        self.assertEqual(field.preset_components(), ("51.272833", "-0.792044"))

        field.set_components("", "")
        self.assertEqual(field.text(), "")
        self.assertEqual(field.preset_components(), ("", ""))


if __name__ == "__main__":
    unittest.main()
