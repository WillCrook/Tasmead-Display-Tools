import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QMessageBox

from pages.debris_page import DebrisPage
from pages.debris_ui import DebrisPresetManagerDialog, DebrisPreviewDialog
from services import DebrisSimulationResult
from workers import SimulationFailure


class DebrisUiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

        def app_data(relative=""):
            return str(self.root / "app-data" / relative)

        def resource(relative=""):
            return str(self.root / "resources" / relative)

        self.app_patch = patch("pages.debris_page.app_data_path", app_data)
        self.resource_patch = patch("pages.debris_page.resource_path", resource)
        self.app_patch.start()
        self.resource_patch.start()
        self.page = DebrisPage()

    def tearDown(self):
        self.page.close()
        self.resource_patch.stop()
        self.app_patch.stop()
        self.temp_dir.cleanup()


class DebrisWorkspaceTests(DebrisUiTestCase):
    def test_modern_two_column_workspace_and_compact_preset_toolbar(self):
        self.assertEqual(self.page.objectName(), "DebrisPage")
        self.assertEqual(self.page.splitter.count(), 2)
        self.assertFalse(self.page.splitter.childrenCollapsible())
        self.assertEqual(self.page.config_widget.objectName(), "workspacePanel")
        self.assertEqual(self.page.flight_input_card.objectName(), "workspacePanel")
        self.assertEqual(self.page.results_widget.objectName(), "resultsCard")
        self.assertEqual(self.page.preset_combo.itemText(0), "Choose a debris preset")
        self.assertEqual(self.page.apply_preset_btn.text(), "Apply")
        self.assertEqual(self.page.save_preset_btn.text(), "Save current…")
        self.assertEqual(self.page.manage_presets_btn.text(), "Manage presets…")
        self.assertEqual(self.page.run_btn.objectName(), "primaryButton")
        self.assertFalse(self.page.preview_btn.isEnabled())

    def test_action_buttons_use_local_icons(self):
        for button in (self.page.manage_presets_btn, self.page.preview_btn):
            with self.subTest(button=button.text()):
                self.assertFalse(button.icon().isNull())

    def test_mode_selector_keeps_stored_values_and_only_mounts_active_form(self):
        cases = (
            (self.page.rb_kml, "kml", None),
            (self.page.rb_coords, "coords", self.page.coords_container),
            (self.page.rb_bearing, "bearing", self.page.bearing_container),
        )
        for button, expected_mode, expected_widget in cases:
            with self.subTest(expected_mode=expected_mode):
                button.click()
                self.assertEqual(self.page.flight_mode, expected_mode)
                mounted = self.page.mode_stack_layout.itemAt(0)
                if expected_widget is None:
                    self.assertIsNone(mounted)
                    self.assertFalse(self.page.kml_container.isHidden())
                else:
                    self.assertIs(mounted.widget(), expected_widget)
                    self.assertTrue(self.page.kml_container.isHidden())

    def test_success_enables_preview_and_later_failure_keeps_last_output(self):
        output = self.root / "trajectory.kml"
        result = DebrisSimulationResult(
            heading=90.0,
            air_distance_m=10.0,
            ground_distance_m=2.0,
            total_distance_m=12.0,
            impacts=1,
            output_file=str(output),
        )
        self.page._terminal_outcome = ("success", result)
        with patch.object(QMessageBox, "information"):
            self.page._on_simulation_thread_finished()

        self.assertTrue(self.page.preview_btn.isEnabled())
        self.assertEqual(self.page._last_successful_output, str(output))

        self.page._terminal_outcome = (
            "failure",
            SimulationFailure("RuntimeError", "failed", ""),
        )
        with patch.object(QMessageBox, "exec", return_value=0):
            self.page._on_simulation_thread_finished()

        self.assertTrue(self.page.preview_btn.isEnabled())
        self.assertEqual(self.page._last_successful_output, str(output))

    def test_preview_action_opens_fullscreen_shell_for_last_output(self):
        output = self.root / "trajectory.kml"
        self.page._last_successful_output = str(output)
        self.page.preview_btn.setEnabled(True)
        with patch("pages.debris_page.DebrisPreviewDialog") as dialog_class:
            self.page.preview_btn.click()

        dialog_class.assert_called_once_with(str(output), self.page)
        dialog_class.return_value.showFullScreen.assert_called_once_with()


class DebrisPreviewDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_dialog_identifies_output_and_escape_closes_it(self):
        dialog = DebrisPreviewDialog("/tmp/generated trajectory.kml")
        self.assertEqual(dialog.output_path, Path("/tmp/generated trajectory.kml"))
        self.assertFalse(dialog.close_button.icon().isNull())
        dialog.showFullScreen()
        self.app.processEvents()
        self.assertTrue(dialog.isFullScreen())
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        self.app.processEvents()
        self.assertFalse(dialog.isVisible())


class DebrisPresetManagerTests(DebrisUiTestCase):
    def make_dialog(self):
        return DebrisPresetManagerDialog(
            self.page.preset_repository,
            self.page.preset_transfer,
            self.page,
        )

    def test_manager_searches_and_confirms_before_delete(self):
        keep = self.page.preset_repository.create("Keep", {"value": 1})
        remove = self.page.preset_repository.create("Remove", {"value": 2})
        dialog = self.make_dialog()
        dialog.search_input.setText("remove")
        self.assertEqual(dialog.preset_list.count(), 1)
        self.assertEqual(dialog.preset_list.item(0).text(), "Remove")
        dialog.preset_list.setCurrentRow(0)

        with patch.object(
            QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.Cancel,
        ):
            dialog.delete_preset()
        self.assertIsNotNone(self.page.preset_repository.get(remove.preset.id))

        with patch.object(
            QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            dialog.delete_preset()
        self.assertIsNone(self.page.preset_repository.get(remove.preset.id))
        self.assertIsNotNone(self.page.preset_repository.get(keep.preset.id))
        dialog.close()


if __name__ == "__main__":
    unittest.main()
