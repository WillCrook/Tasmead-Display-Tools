import os
import sys
import unittest
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from PyQt6.QtWidgets import QApplication, QLineEdit

from pages.unit_fields import MetreFeetFieldPair


class MetreFeetFieldPairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.metres = QLineEdit()
        self.feet = QLineEdit()
        self.changes = []
        self.pair = MetreFeetFieldPair(
            self.metres,
            self.feet,
            on_metres_changed=lambda: self.changes.append(self.metres.text()),
        )

    def test_converts_in_both_directions_with_existing_precision(self):
        self.metres.setText("10")
        self.assertEqual(self.feet.text(), "32.81")
        self.assertEqual(self.changes, ["10"])

        self.feet.setText("65.6168")
        self.assertEqual(self.metres.text(), "20.00")
        self.assertEqual(self.changes, ["10", "20.00"])

    def test_empty_and_invalid_sources_clear_the_counterpart(self):
        self.metres.setText("10")
        self.metres.clear()
        self.assertEqual(self.feet.text(), "")

        self.feet.setText("not a number")
        self.assertEqual(self.metres.text(), "")

    def test_programmatic_setters_can_suppress_dependent_callback(self):
        self.pair.set_metres_text("12.5", notify_dependents=False)
        self.assertEqual(self.metres.text(), "12.5")
        self.assertEqual(self.feet.text(), "41.01")
        self.assertEqual(self.changes, [])

        self.pair.set_feet_text("82.021", notify_dependents=False)
        self.assertEqual(self.metres.text(), "25.00")
        self.assertEqual(self.feet.text(), "82.021")
        self.assertEqual(self.changes, [])

    def test_numeric_setter_formats_both_fields_from_the_unrounded_value(self):
        self.pair.set_metres_value(1.005, notify_dependents=False)
        self.assertEqual(self.metres.text(), "1.00")
        self.assertEqual(self.feet.text(), "3.30")
        self.assertEqual(self.changes, [])


if __name__ == "__main__":
    unittest.main()
