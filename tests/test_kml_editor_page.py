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
from PyQt6.QtGui import QKeySequence
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QFileDialog, QMessageBox

from pages.kml_editor_page import KmlEditorPage
from services import EditorMode, ParseStatus


VALID_KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><gx:Track xmlns:gx="http://www.google.com/kml/ext/2.2">
<when>2026-01-01T00:00:00Z</when><when>2026-01-01T00:00:01Z</when><when>2026-01-01T00:00:02Z</when>
<gx:coord>-1 51 10</gx:coord><gx:coord>-1.1 51.1 20</gx:coord><gx:coord>-1.2 51.2 30</gx:coord>
</gx:Track></Placemark></kml>
"""


class KmlEditorPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.first = self.root / "first.kml"
        self.second = self.root / "second.kml"
        self.first.write_text(VALID_KML, encoding="utf-8")
        self.second.write_text(VALID_KML.replace("-1.2 51.2 30", "-2 52 30"), encoding="utf-8")
        self.page = KmlEditorPage()
        self.page.resize(900, 560)
        self.page.show()
        self.app.processEvents()

    def tearDown(self):
        self.page.close()
        self.temp_dir.cleanup()

    def _add(self, *paths):
        result = self.page.model.add_paths(paths)
        self.app.processEvents()
        return result.document_ids

    def test_browse_adds_multiple_files_with_remembered_location(self):
        with (
            patch("pages.kml_editor_page.remembered_directory", return_value=str(self.root)) as remembered,
            patch.object(QFileDialog, "getOpenFileNames", return_value=([str(self.first), str(self.second)], "")),
            patch("pages.kml_editor_page.remember_file_selection") as remember,
        ):
            self.page.browse_files()

        self.assertEqual(self.page.file_list.count(), 2)
        self.assertEqual(self.page.file_count_label.text(), "2 files")
        remembered.assert_called_once()
        remember.assert_called_once()
        self.assertEqual(self.page.model.active_document.source_path, self.first.resolve())

    def test_active_switching_renders_isolated_text_and_dirty_marker(self):
        first_id, second_id = self._add(self.first, self.second)
        first_contents = self.page.model.document(first_id).contents
        self.page.text_editor.setPlainText(first_contents + "<!-- first edit -->")
        self.app.processEvents()
        self.assertTrue(self.page.model.document(first_id).dirty)
        self.assertTrue(self.page.file_list.item(0).text().endswith(" *"))

        self.page.file_list.setCurrentRow(1)
        self.app.processEvents()

        self.assertEqual(self.page.model.active_document_id, second_id)
        self.assertEqual(self.page.text_editor.toPlainText(), self.page.model.document(second_id).contents)
        self.assertFalse(self.page.model.document(second_id).dirty)
        self.page.file_list.setCurrentRow(0)
        self.assertIn("first edit", self.page.text_editor.toPlainText())

    def test_modes_crop_range_simplification_and_stale_disable_are_model_backed(self):
        document_id = self._add(self.first)[0]
        self.assertTrue(self.page.crop_start_slider.isEnabled())
        self.assertIn("2026-01-01T00:00:00Z", self.page.crop_start_label.text())

        self.page.crop_mode_button.click()
        self.page.crop_start_slider.setValue(1)
        self.assertEqual(self.page.model.mode, EditorMode.CROP)
        self.assertEqual(self.page.workspace_stack.currentWidget(), self.page.crop_page)
        self.assertEqual(self.page.model.document(document_id).crop_state.start_index, 1)

        self.page.simplify_mode_button.click()
        self.page.tolerance_input.setValue(42.5)
        self.assertEqual(self.page.model.mode, EditorMode.SIMPLIFY)
        self.assertEqual(
            self.page.model.document(document_id).simplification_state.tolerance_m,
            42.5,
        )
        self.assertEqual(self.page.original_points_label.text(), "Original points: 3")

        self.page.text_mode_button.click()
        self.page.text_editor.insertPlainText("<!-- stale -->")
        self.app.processEvents()
        self.assertEqual(self.page.model.document(document_id).parse_state.status, ParseStatus.STALE)
        self.assertFalse(self.page.crop_start_slider.isEnabled())

    def test_crop_sliders_preserve_an_ordered_range(self):
        document_id = self._add(self.first)[0]
        self.page.crop_end_slider.setValue(1)
        self.page.crop_start_slider.setValue(2)

        crop = self.page.model.document(document_id).crop_state
        self.assertEqual((crop.start_index, crop.end_index), (2, 2))

    def test_source_save_cancel_restore_and_save_anyway_branches(self):
        document_id = self._add(self.first)[0]
        saved = self.page.model.document(document_id).saved_contents
        changed = saved + "<!-- changed -->"
        self.page.text_editor.setPlainText(changed)

        with patch.object(self.page, "_confirm_unvalidated_source_save", return_value="cancel"):
            self.assertFalse(self.page.save_active_document())
        self.assertNotIn("changed", self.first.read_text(encoding="utf-8"))

        with patch.object(self.page, "_confirm_unvalidated_source_save", return_value="restore"):
            self.assertTrue(self.page.save_active_document())
        self.assertEqual(self.page.model.document(document_id).contents, saved)
        self.assertFalse(self.page.model.document(document_id).dirty)

        self.page.text_editor.setPlainText(changed)
        with patch.object(self.page, "_confirm_unvalidated_source_save", return_value="save"):
            self.assertTrue(self.page.save_active_document())
        self.assertIn("changed", self.first.read_text(encoding="utf-8"))
        self.assertFalse(self.page.model.document(document_id).dirty)

    def test_save_as_does_not_show_stale_warning_and_updates_active_path(self):
        document_id = self._add(self.first)[0]
        self.page.text_editor.insertPlainText("<!-- changed -->")
        destination = self.root / "copy"
        with (
            patch.object(QFileDialog, "getSaveFileName", return_value=(str(destination), "")),
            patch.object(self.page, "_confirm_unvalidated_source_save") as warning,
            patch("pages.kml_editor_page.remember_file_selection") as remember,
        ):
            self.assertTrue(self.page.save_active_document_as())

        warning.assert_not_called()
        remember.assert_called_once()
        self.assertEqual(self.page.model.document(document_id).source_path, destination.with_suffix(".kml").resolve())
        self.assertTrue(destination.with_suffix(".kml").exists())

    def test_save_as_to_source_path_uses_source_validation_warning(self):
        self._add(self.first)
        self.page.text_editor.insertPlainText("<!-- changed -->")
        with (
            patch.object(QFileDialog, "getSaveFileName", return_value=(str(self.first), "")),
            patch.object(
                self.page,
                "_confirm_unvalidated_source_save",
                return_value="cancel",
            ) as warning,
        ):
            self.assertFalse(self.page.save_active_document_as())
        warning.assert_called_once()
        self.assertNotIn("changed", self.first.read_text(encoding="utf-8"))

    def test_dirty_remove_and_close_cover_cancel_discard_and_save(self):
        document_id = self._add(self.first)[0]
        self.page.text_editor.insertPlainText("<!-- changed -->")
        self.page.file_list.item(0).setSelected(True)

        with patch.object(self.page, "_ask_unsaved", return_value="cancel"):
            self.assertFalse(self.page.remove_selected_files())
        self.assertEqual(len(self.page.model.documents), 1)

        with patch.object(self.page, "_ask_unsaved", return_value="discard"):
            self.assertTrue(self.page.confirm_close())
        self.assertTrue(self.page.model.document(document_id).dirty)

        with (
            patch.object(self.page, "_ask_unsaved", return_value="save"),
            patch.object(self.page, "_confirm_unvalidated_source_save", return_value="save"),
        ):
            self.assertTrue(self.page.confirm_close())
        self.assertFalse(self.page.model.document(document_id).dirty)

    def test_restore_button_requires_confirmation(self):
        document_id = self._add(self.first)[0]
        saved = self.page.model.document(document_id).saved_contents
        self.page.text_editor.insertPlainText("<!-- changed -->")

        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Cancel):
            self.assertFalse(self.page.restore_active_document())
        self.assertTrue(self.page.model.document(document_id).dirty)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.assertTrue(self.page.restore_active_document())
        self.assertEqual(self.page.model.document(document_id).contents, saved)

    def test_accessible_names_focus_order_and_shortcuts_are_present(self):
        self._add(self.first)
        self.assertEqual(self.page.file_list.accessibleName(), "KML Editor input files")
        self.assertEqual(self.page.mode_control.accessibleName(), "KML editor mode")
        self.assertEqual(self.page.text_editor.accessibleName(), "Editable KML contents")
        self.assertEqual(self.page.crop_start_slider.accessibleName(), "Crop start point")
        self.assertEqual(
            self.page.tolerance_input.accessibleName(),
            "Simplification tolerance metres",
        )
        self.assertEqual(self.page.open_shortcut.key(), QKeySequence(QKeySequence.StandardKey.Open))
        self.assertEqual(self.page.save_shortcut.key(), QKeySequence(QKeySequence.StandardKey.Save))
        self.page.file_list.setFocus()
        QTest.keyClick(self.page.file_list, Qt.Key.Key_Tab)
        self.assertTrue(self.page.add_files_btn.hasFocus())


if __name__ == "__main__":
    unittest.main()
