import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QFontDatabase, QKeySequence, QTextCursor
from PyQt6.QtTest import QSignalSpy, QTest
from PyQt6.QtWidgets import QApplication, QFileDialog, QMessageBox, QPlainTextEdit

from google_maps_settings import GoogleMapsSettings
from pages.crop_range_slider import CropRangeSlider
from pages.kml_editor_page import KmlEditorPage
from services import EditorMode, KmlSourceLocation, ParseStatus


VALID_KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><gx:Track xmlns:gx="http://www.google.com/kml/ext/2.2">
<when>2026-01-01T00:00:00Z</when><when>2026-01-01T00:00:01Z</when><when>2026-01-01T00:00:02Z</when>
<gx:coord>-1 51 10</gx:coord><gx:coord>-1.1 51.1 20</gx:coord><gx:coord>-1.2 51.2 30</gx:coord>
</gx:Track></Placemark></kml>
"""


class MemorySettings:
    def __init__(self, initial=None):
        self.values = dict(initial or {})
        self.sync_count = 0

    def value(self, key, default=None, type=None):
        value = self.values.get(key, default)
        return type(value) if type is not None else value

    def setValue(self, key, value):
        self.values[key] = value

    def sync(self):
        self.sync_count += 1


class CropRangeSliderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.slider = CropRangeSlider()
        self.slider.resize(360, 32)
        self.slider.set_range(0, 10)
        self.slider.set_values(2, 8)
        self.slider.show()
        self.app.processEvents()

    def tearDown(self):
        self.slider.close()

    def test_keyboard_selects_and_moves_each_handle_without_crossing(self):
        changes = QSignalSpy(self.slider.range_changed)
        finished = QSignalSpy(self.slider.interaction_finished)
        self.slider.setFocus()

        QTest.keyClick(self.slider, Qt.Key.Key_Right)
        self.assertEqual(self.slider.values(), (3, 8))
        QTest.keyClick(self.slider, Qt.Key.Key_Space)
        QTest.keyClick(self.slider, Qt.Key.Key_Left)
        self.assertEqual(self.slider.values(), (3, 7))

        self.slider.set_values(6, 7)
        QTest.keyClick(self.slider, Qt.Key.Key_Left)
        self.assertEqual(self.slider.values(), (6, 7))
        self.assertGreaterEqual(len(changes), 2)
        self.assertGreaterEqual(len(finished), 3)

    def test_mouse_drag_emits_one_ordered_range_and_completion(self):
        changes = QSignalSpy(self.slider.range_changed)
        finished = QSignalSpy(self.slider.interaction_finished)
        upper = self.slider._handle_rect(self.slider.upper_value()).center()
        target_x = self.slider._handle_rect(6).center().x()

        QTest.mousePress(self.slider, Qt.MouseButton.LeftButton, pos=upper)
        QTest.mouseMove(self.slider, QPoint(target_x, upper.y()))
        QTest.mouseRelease(
            self.slider,
            Qt.MouseButton.LeftButton,
            pos=QPoint(target_x, upper.y()),
        )

        lower, upper_value = self.slider.values()
        self.assertLess(lower, upper_value)
        self.assertTrue(changes)
        self.assertEqual(len(finished), 1)

    def test_accessibility_and_progress_geometry_cover_the_retained_interval(self):
        self.slider.setAccessibleName("Retained crop range")
        lower = self.slider._handle_rect(self.slider.lower_value()).center().x()
        upper = self.slider._handle_rect(self.slider.upper_value()).center().x()

        self.assertEqual(self.slider.accessibleName(), "Retained crop range")
        self.assertLess(lower, upper)
        self.assertGreaterEqual(self.slider.minimumSizeHint().width(), 120)


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
        self.settings = MemorySettings()
        self.maps_storage = MemorySettings()
        self.maps_settings = GoogleMapsSettings(settings=self.maps_storage)
        self.page = KmlEditorPage(
            settings=self.settings,
            maps_settings=self.maps_settings,
        )
        self.page.resize(900, 560)
        self.page.show()
        self.app.processEvents()

    def tearDown(self):
        self.page.model._validation_pool.waitForDone(1000)
        self.page.model._operation_executor.wait_for_done(1.0)
        self.app.processEvents()
        self.page.shutdown()
        self.page.close()
        self.temp_dir.cleanup()

    def _add(self, *paths):
        result = self.page.model.add_paths(paths)
        self._wait_for_validation(*result.document_ids)
        return result.document_ids

    def _wait_for_validation(self, *document_ids, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if all(
                self.page.model.document(document_id).parse_state.status
                not in {ParseStatus.STALE, ParseStatus.VALIDATING}
                for document_id in document_ids
            ):
                return
            QTest.qWait(5)
        self.fail("validation did not finish")

    def _wait_for_operation(self, document_id, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if not any(key[0] == document_id for key in self.page.model._operation_tasks):
                return
            QTest.qWait(5)
        self.fail("editor operation did not finish")

    def test_browse_adds_multiple_files_with_remembered_location(self):
        with (
            patch("pages.kml_editor_page.remembered_directory", return_value=str(self.root)) as remembered,
            patch.object(QFileDialog, "getOpenFileNames", return_value=([str(self.first), str(self.second)], "")),
            patch("pages.kml_editor_page.remember_file_selection") as remember,
        ):
            self.page.browse_files()

        self._wait_for_validation(
            *(document.document_id for document in self.page.model.documents)
        )

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
        self.assertTrue(self.page.crop_range_slider.isEnabled())
        self.assertIn("2026-01-01T00:00:00Z", self.page.crop_start_label.text())

        self.page.crop_mode_button.click()
        self.page.crop_range_slider.set_values(1, 2, emit=True)
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
        self.assertFalse(self.page.crop_range_slider.isEnabled())

    def test_crop_range_control_updates_both_model_boundaries(self):
        document_id = self._add(self.first)[0]
        self.page.crop_range_slider.set_values(1, 2, emit=True)

        crop = self.page.model.document(document_id).crop_state
        self.assertEqual((crop.start_index, crop.end_index), (1, 2))

    def test_crop_summary_updates_and_live_preview_stays_embedded(self):
        document_id = self._add(self.first)[0]
        scenes = []
        self.page.preview_requested.connect(scenes.append)
        self.maps_settings.set_api_key("test-key")

        with patch.object(
            self.page.crop_map_preview,
            "set_scene",
            return_value=True,
        ) as set_scene:
            self.page.crop_mode_button.click()
            self.page.crop_range_slider.set_values(1, 2, emit=True)
            self.page.crop_range_slider.interaction_finished.emit()
            self._wait_for_operation(document_id)
            self.app.processEvents()

        self.assertIn("2026-01-01T00:00:01Z", self.page.crop_start_label.text())
        self.assertIn("Retained points: 2 of 3", self.page.crop_count_label.text())
        self.assertEqual(scenes, [])
        set_scene.assert_called()
        scene, api_key, presentation = set_scene.call_args.args
        self.assertEqual(api_key, "test-key")
        self.assertTrue(presentation.embedded)
        self.assertFalse(presentation.measurement_enabled)
        self.assertFalse(presentation.controls_panel_visible)
        self.assertEqual(presentation.ready_message, "")
        self.assertEqual(presentation.legend, ())
        self.assertEqual(len(scene.traces[0].base_document.placemarks), 2)
        self.assertFalse(hasattr(self.page, "crop_timing_label"))
        self.assertEqual(self.page.crop_status_label.text(), "")

        actions = self.page.crop_page.layout().itemAt(
            self.page.crop_page.layout().count() - 1
        ).layout()
        self.assertIs(actions.itemAt(0).widget(), self.page.crop_reset_btn)
        self.assertIs(actions.itemAt(1).widget(), self.page.crop_recentre_btn)
        self.assertIs(
            self.page.crop_reset_btn.nextInFocusChain(),
            self.page.crop_recentre_btn,
        )
        with patch.object(self.page.crop_map_preview, "_run_javascript") as run_script:
            self.page.crop_recentre_btn.click()
        run_script.assert_called_once_with("window.tasmead.fitScene();")

    def test_crop_preview_is_revealed_before_deferred_webengine_start(self):
        document_id = self._add(self.first)[0]
        self.maps_settings.set_api_key("test-key")
        scene = object()

        with (
            patch.object(self.page.model, "request_crop_preview", return_value=False),
            patch.object(
                self.page.crop_map_preview,
                "set_scene",
                return_value=True,
            ) as set_scene,
        ):
            self.page.model.set_mode(EditorMode.CROP)
            self.page._present_crop_scene(document_id, scene)

            self.assertIs(
                self.page.crop_preview_stack.currentWidget(),
                self.page.crop_map_preview,
            )
            self.assertTrue(self.page._crop_preview_start_timer.isActive())
            set_scene.assert_not_called()

            self.app.processEvents()

            set_scene.assert_called_once()
            self.assertIs(set_scene.call_args.args[0], scene)

    def test_deferred_crop_preview_coalesces_to_latest_scene(self):
        document_id = self._add(self.first)[0]
        self.maps_settings.set_api_key("test-key")
        first_scene = object()
        latest_scene = object()

        with (
            patch.object(self.page.model, "request_crop_preview", return_value=False),
            patch.object(
                self.page.crop_map_preview,
                "set_scene",
                return_value=True,
            ) as set_scene,
        ):
            self.page.model.set_mode(EditorMode.CROP)
            self.page._present_crop_scene(document_id, first_scene)
            self.page._present_crop_scene(document_id, latest_scene)
            self.app.processEvents()

            set_scene.assert_called_once()
            self.assertIs(set_scene.call_args.args[0], latest_scene)

    def test_deferred_crop_preview_is_cancelled_by_placeholder_mode_and_document(self):
        first_id, second_id = self._add(self.first, self.second)
        self.maps_settings.set_api_key("test-key")

        with (
            patch.object(self.page.model, "request_crop_preview", return_value=False),
            patch.object(
                self.page.crop_map_preview,
                "set_scene",
                return_value=True,
            ) as set_scene,
        ):
            self.page.model.set_mode(EditorMode.CROP)

            self.page._present_crop_scene(first_id, object())
            self.page._set_crop_preview_placeholder("Cancelled")
            self.app.processEvents()
            set_scene.assert_not_called()

            self.page._present_crop_scene(first_id, object())
            self.page.model.set_mode(EditorMode.TEXT)
            self.app.processEvents()
            set_scene.assert_not_called()

            self.page.model.set_mode(EditorMode.CROP)
            self.page._present_crop_scene(first_id, object())
            self.page.model.set_active_document(second_id)
            self.app.processEvents()
            set_scene.assert_not_called()

    def test_crop_preview_debounces_to_the_latest_range(self):
        document_id = self._add(self.first)[0]
        self.maps_settings.set_api_key("test-key")

        with patch.object(
            self.page.model,
            "request_crop_preview",
            return_value=True,
        ) as request_preview:
            self.page.crop_mode_button.click()
            request_preview.reset_mock()
            self.page.crop_range_slider.set_values(0, 1, emit=True)
            self.page.crop_range_slider.set_values(1, 2, emit=True)
            QTest.qWait(130)

        request_preview.assert_called_once_with(document_id)
        crop = self.page.model.document(document_id).crop_state
        self.assertEqual((crop.start_index, crop.end_index), (1, 2))

    def test_crop_callbacks_report_unexpected_errors_without_escaping(self):
        self._add(self.first)
        self.maps_settings.set_api_key("test-key")

        with (
            patch.object(
                self.page.model,
                "request_crop_preview",
                side_effect=RuntimeError("preview boom"),
            ),
            self.assertLogs("pages.kml_editor_page", level="ERROR") as preview_logs,
        ):
            self.page.model.set_mode(EditorMode.CROP)

        self.assertIn("Crop preview failed: preview boom", self.page.crop_status_label.text())
        self.assertIn("preview boom", "\n".join(preview_logs.output))

        with (
            patch.object(
                self.page.model,
                "apply_crop",
                side_effect=RuntimeError("apply boom"),
            ),
            self.assertLogs("pages.kml_editor_page", level="ERROR") as apply_logs,
        ):
            self.assertFalse(self.page.apply_crop())

        self.assertIn("Apply Crop failed: apply boom", self.page.crop_status_label.text())
        self.assertIn("apply boom", "\n".join(apply_logs.output))

    def test_missing_maps_key_is_inline_and_does_not_disable_apply(self):
        self._add(self.first)
        settings_requests = QSignalSpy(self.page.maps_settings_requested)
        self.page.crop_mode_button.click()

        self.assertIs(
            self.page.crop_preview_stack.currentWidget(),
            self.page.crop_preview_placeholder,
        )
        self.assertIn("Google Maps API key", self.page.crop_preview_placeholder_message.text())
        self.assertFalse(self.page.crop_preview_settings_btn.isHidden())
        self.assertTrue(self.page.crop_apply_btn.isEnabled())
        self.page.crop_preview_settings_btn.click()
        self.assertEqual(len(settings_requests), 1)

    def test_resolution_presets_report_counts_and_custom_is_positive(self):
        document_id = self._add(self.first)[0]
        self.page.simplify_mode_button.click()
        self._wait_for_operation(document_id)
        self.app.processEvents()

        self.assertTrue(self.page.simplification_preset_buttons["balanced"].isChecked())
        self.assertEqual(self.page.tolerance_input.minimum(), 0.01)
        self.assertIn("Resulting points:", self.page.result_points_label.text())
        self.page.simplification_preset_buttons["aggressive"].click()
        self.assertEqual(
            self.page.model.document(document_id).simplification_state.tolerance_m,
            20.0,
        )

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
        self.assertEqual(self.page.crop_range_slider.accessibleName(), "Retained crop range")
        self.assertEqual(
            self.page.tolerance_input.accessibleName(),
            "Custom maximum path deviation metres",
        )
        self.assertEqual(self.page.open_shortcut.key(), QKeySequence(QKeySequence.StandardKey.Open))
        self.assertEqual(self.page.save_shortcut.key(), QKeySequence(QKeySequence.StandardKey.Save))
        self.page.file_list.setFocus()
        QTest.keyClick(self.page.file_list, Qt.Key.Key_Tab)
        self.assertTrue(self.page.add_files_btn.hasFocus())

    def test_code_editor_is_fixed_width_no_wrap_and_bounds_long_line_highlighting(self):
        self.assertEqual(
            self.page.text_editor.lineWrapMode(),
            QPlainTextEdit.LineWrapMode.NoWrap,
        )
        self.assertEqual(
            self.page.text_editor.font().family(),
            QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family(),
        )
        long_line = "<coordinates>" + ("1,2,3 " * 400_000) + "</coordinates>"
        self.page.text_editor.setPlainText(long_line)
        self.app.processEvents()
        self.assertEqual(self.page.text_editor.toPlainText(), long_line)
        self.assertGreater(self.page.text_editor.line_number_area_width(), 0)
        self.assertLessEqual(
            self.page.text_editor.highlighter._last_scan_length,
            self.page.text_editor.highlighter.MAX_HIGHLIGHT_CHARS,
        )
        self.page.text_editor.setPlainText("😀\n68")
        self.assertTrue(
            self.page.text_editor.go_to_location(
                KmlSourceLocation(line=2, column=1, offset=2, length=2)
            )
        )
        self.assertEqual(self.page.text_editor.textCursor().selectedText(), "68")

    def test_coordinate_formatting_is_visible_undoable_and_revalidates_location(self):
        fixture = PROJECT_ROOT / "tests" / "fixtures" / "kml" / "standalone_coordinate_token.kml"
        original = fixture.read_text(encoding="utf-8")
        document_id = self._add(fixture)[0]

        self.assertTrue(self.page.format_active_coordinates())
        formatted = self.page.text_editor.toPlainText()
        coordinate_body = formatted.split("<coordinates>", 1)[1].split("</coordinates>", 1)[0]
        lines = [line.strip() for line in coordinate_body.splitlines() if line.strip()]
        self.assertEqual(lines[0], "68 12.417867,41.670955,193.099038")
        self.assertEqual(lines[1], "12.418224,41.670881,191.801193")
        self.assertTrue(self.page.model.document(document_id).dirty)
        self.assertEqual(fixture.read_text(encoding="utf-8"), original)

        self._wait_for_validation(document_id)
        diagnostic = self.page.model.document(document_id).parse_state.diagnostics[0]
        expected_line = formatted.count("\n", 0, formatted.index("68")) + 1
        self.assertEqual(diagnostic.location.line, expected_line)
        self.assertTrue(self.page.go_to_selected_diagnostic())
        self.assertEqual(self.page.text_editor.textCursor().selectedText(), "68")

        self.page.text_editor.undo()
        self.app.processEvents()
        self.assertEqual(self.page.text_editor.toPlainText(), original)
        self.assertFalse(self.page.model.document(document_id).dirty)

    def test_coordinate_formatting_only_changes_the_active_document(self):
        first_id, second_id = self._add(self.first, self.second)
        second_contents = self.page.model.document(second_id).contents
        self.assertTrue(self.page.format_active_coordinates())
        self.assertTrue(self.page.model.document(first_id).dirty)
        self.assertFalse(self.page.model.document(second_id).dirty)
        self.assertEqual(self.page.model.document(second_id).contents, second_contents)

    def test_font_size_controls_persist_without_changing_document_state(self):
        document_id = self._add(self.first)[0]
        document = self.page.model.document(document_id)
        original_revision = document.revision
        original_size = self.page.font_size_input.value()
        original_gutter_width = self.page.text_editor.line_number_area_width()

        self.page.font_increase_btn.click()
        increased = min(32, original_size + 1)
        self.assertEqual(self.page.font_size_input.value(), increased)
        self.assertEqual(self.page.text_editor.font().pointSize(), increased)
        self.assertEqual(self.settings.values["kml-editor/font-size"], increased)
        self.assertEqual(self.page.model.document(document_id).revision, original_revision)
        self.assertFalse(self.page.model.document(document_id).dirty)
        self.assertGreaterEqual(
            self.page.text_editor.line_number_area_width(),
            original_gutter_width,
        )

        self.page.font_decrease_shortcut.activated.emit()
        self.assertEqual(self.page.font_size_input.value(), original_size)
        self.page.font_size_input.setValue(32)
        self.page.font_increase_shortcut.activated.emit()
        self.assertEqual(self.page.font_size_input.value(), 32)
        self.page.font_reset_shortcut.activated.emit()
        self.assertEqual(
            self.page.font_size_input.value(),
            self.page._default_editor_font_size,
        )

        restored = KmlEditorPage(settings=self.settings)
        try:
            self.assertEqual(restored.font_size_input.value(), self.page._default_editor_font_size)
            self.assertEqual(
                restored.text_editor.font().pointSize(),
                self.page._default_editor_font_size,
            )
        finally:
            restored.model._validation_pool.waitForDone(1000)
            restored.close()

    def test_line_number_gutter_reserves_space_for_text_at_active_font(self):
        self.page.text_editor.setPlainText("\n".join("value" for _ in range(12_345)))
        self.page.font_size_input.setValue(24)
        self.page.text_editor.resize(700, 300)
        self.app.processEvents()

        editor = self.page.text_editor
        expected_text_width = editor.fontMetrics().horizontalAdvance("12345")
        self.assertGreaterEqual(
            editor.line_number_area_width(),
            expected_text_width
            + editor.GUTTER_LEFT_PADDING
            + editor.GUTTER_RIGHT_PADDING
            + editor.GUTTER_SEPARATOR_WIDTH,
        )
        self.assertLess(
            editor.line_number_area.geometry().right(),
            editor.viewport().geometry().left(),
        )

    def test_search_wraps_and_reports_missing_text(self):
        self._add(self.first)
        self.page.show_search()
        self.page.search_input.setText("gx:coord")
        self.assertEqual(self.page.text_editor.textCursor().selectedText(), "gx:coord")
        cursor = self.page.text_editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.page.text_editor.setTextCursor(cursor)
        self.assertTrue(self.page.find_next())
        self.assertEqual(self.page.text_editor.textCursor().selectedText(), "gx:coord")
        self.page.search_input.setText("not-present-in-document")
        self.assertEqual(self.page.search_status_label.text(), "No matches")
        self.page.close_search()
        self.assertFalse(self.page.search_bar.isVisible())

    def test_diagnostic_navigation_selects_the_invalid_coordinate_token(self):
        fixture = PROJECT_ROOT / "tests" / "fixtures" / "kml" / "standalone_coordinate_token.kml"
        document_id = self._add(fixture)[0]
        document = self.page.model.document(document_id)
        self.assertEqual(document.parse_state.status, ParseStatus.INVALID)
        self.assertEqual(self.page.diagnostic_list.topLevelItemCount(), 1)
        self.assertIn("wrong number", self.page.diagnostic_list.topLevelItem(0).text(2))
        self.assertTrue(self.page.go_to_selected_diagnostic())
        self.assertEqual(self.page.text_editor.textCursor().selectedText(), "68")
        self.assertFalse(hasattr(document.parse_state.diagnostics[0], "replacement"))

    def test_switching_files_restores_each_cursor_position(self):
        first_id, second_id = self._add(self.first, self.second)
        cursor = self.page.text_editor.textCursor()
        cursor.setPosition(40)
        self.page.text_editor.setTextCursor(cursor)
        self.page.file_list.setCurrentRow(1)
        cursor = self.page.text_editor.textCursor()
        cursor.setPosition(70)
        self.page.text_editor.setTextCursor(cursor)

        self.page.file_list.setCurrentRow(0)
        self.assertEqual(self.page.model.active_document_id, first_id)
        self.assertEqual(self.page.text_editor.textCursor().position(), 40)
        self.page.file_list.setCurrentRow(1)
        self.assertEqual(self.page.model.active_document_id, second_id)
        self.assertEqual(self.page.text_editor.textCursor().position(), 70)


if __name__ == "__main__":
    unittest.main()
