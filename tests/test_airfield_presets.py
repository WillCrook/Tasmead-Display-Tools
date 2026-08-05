import os
import sys
import unittest
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from services import (
    AirfieldPresetData,
    AirfieldPresetError,
    CoordinatePair,
    normalise_runway_designator,
)


class RunwayDesignatorTests(unittest.TestCase):
    def test_conventional_boundaries_and_suffixes_are_normalised(self):
        expected = {
            "01": "01",
            "36": "36",
            "09l": "09L",
            "18c": "18C",
            "27R": "27R",
            " 24r ": "24R",
        }
        for value, normalized in expected.items():
            with self.subTest(value=value):
                result = normalise_runway_designator(value)
                self.assertTrue(result.conventional)
                self.assertEqual(result.value, normalized)

    def test_nonstandard_values_are_preserved_not_silently_fixed(self):
        for value in ("00", "37", "6L", "HELIPAD", "06/24", "24X"):
            with self.subTest(value=value):
                result = normalise_runway_designator(value)
                self.assertFalse(result.conventional)
                self.assertEqual(result.value, value)


class AirfieldPresetDataTests(unittest.TestCase):
    def test_canonical_payload_round_trips_as_typed_values(self):
        payload = AirfieldPresetData.validated(
            airfield_name="Farnborough",
            runway="24l",
            threshold=CoordinatePair(51.272833, -0.792044),
            true_heading_deg="450",
            elevation_m="38",
        )
        self.assertEqual(payload.runway, "24L")
        self.assertEqual(payload.true_heading_deg, 90.0)
        decoded, warnings = AirfieldPresetData.from_mapping(payload.to_mapping())
        self.assertEqual(decoded, payload)
        self.assertEqual(warnings, ())

    def test_legacy_source_fallback_is_not_relabelled_as_airfield_elevation(self):
        decoded, warnings = AirfieldPresetData.from_mapping(
            {
                "name": "Farnborough",
                "latitude": "51.272833",
                "longitude": "-0.792044",
                "heading": "126",
                "original_elevation_m": "38",
            }
        )
        self.assertEqual(decoded.airfield_name, "Farnborough")
        self.assertEqual(decoded.threshold_latitude, 51.272833)
        self.assertIsNone(decoded.elevation_m)
        self.assertTrue(any("fallback elevation" in warning for warning in warnings))

    def test_new_presets_require_all_five_fields(self):
        with self.assertRaisesRegex(AirfieldPresetError, "elevation"):
            AirfieldPresetData.validated(
                airfield_name="Farnborough",
                runway="24",
                threshold=CoordinatePair(51.272833, -0.792044),
                true_heading_deg="240",
                elevation_m="",
            )


if __name__ == "__main__":
    unittest.main()
