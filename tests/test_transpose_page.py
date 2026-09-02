import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import UUID


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QRadioButton,
)

from file_dialog_state import FileDialogDirection, FileDialogWorkflow
from pages.airfield_ui import AirfieldFormValues, AirfieldPresetManagerDialog
from pages.transpose_page import (
    TransposePage,
    TranspositionInputDialog,
    TranspositionOutputDialog,
)
from services import (
    AlignmentMethod,
    DetectionSignalScore,
    KmlPoint,
    KmlStructureError,
    KmlTrack,
    ManualTranspositionAlignment,
    PresetType,
    PreviewScene,
    PreviewTargetSnapshot,
    RunwayCandidate,
    RunwayConfidence,
    RunwayDetectionAssessment,
    RunwayInferenceResult,
    RunwayReference,
    RunwayTranspositionAlignment,
    TranspositionBatchResult,
    TranspositionError,
    TranspositionErrorCode,
    TranspositionFileOutcome,
    TranspositionFileStatus,
    TraceAdjustment,
    TranspositionPresetData,
    create_transposition_plan,
    prepare_transposition,
)


def inferred_track():
    return KmlTrack(
        points=(
            KmlPoint(51.0, -1.0, 30.0),
            KmlPoint(51.01, -0.99, 50.0),
        ),
        geometry_kind="line_string",
        placemark_name="Display",
        altitude_mode="absolute",
    )


def inferred_result(latitude=51.0, longitude=-1.0, heading=90.0, elevation=30.0):
    reference = RunwayReference(latitude, longitude, heading, elevation)
    signals = (
        DetectionSignalScore("Heading consistency", 94, "1.2° median dispersion"),
        DetectionSignalScore("Cross-track fit", 96, "2.5 m at the 95th percentile"),
        DetectionSignalScore("Straightness", 92, "0.980 span-to-path ratio"),
        DetectionSignalScore("Aligned length", 100, "900 m"),
        DetectionSignalScore("Threshold detection", 70, "Existing threshold assessment: medium"),
        DetectionSignalScore("Ground elevation", 88, "Stable direct estimate"),
    )
    assessment = RunwayDetectionAssessment(
        overall_percent=86,
        rating=RunwayConfidence.HIGH,
        heading_percent=95,
        threshold_percent=70,
        ground_elevation_percent=88,
        signals=signals,
        weakest_signal=signals[4],
    )
    return RunwayInferenceResult(
        candidate=RunwayCandidate(
            reference=reference,
            heading_confidence=RunwayConfidence.HIGH,
            threshold_confidence=RunwayConfidence.MEDIUM,
            start_index=0,
            end_index=10,
            aligned_distance_m=900.0,
            heading_dispersion_deg=1.2,
            cross_track_error_m=2.5,
            evidence=("Aligned distance: 900 m", "Altitude change: +20 m"),
            warnings=("Verify the exact threshold.",),
            detection_assessment=assessment,
        )
    )


class TransposePageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

        def app_data(relative=""):
            return str(self.root / "app-data" / relative)

        def resource(relative=""):
            return str(self.root / "resources" / relative)

        self.app_patch = patch("pages.transpose_page.app_data_path", app_data)
        self.resource_patch = patch("pages.transpose_page.resource_path", resource)
        self.app_patch.start()
        self.resource_patch.start()
        self._settings_counter = 0
        self.settings = self.isolated_settings()
        self.page = TransposePage(settings=self.settings)

    def tearDown(self):
        self.page.close()
        self.resource_patch.stop()
        self.app_patch.stop()
        self.tempdir.cleanup()

    def add_inferred_files(self, *paths):
        valid_kml = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><LineString>
<altitudeMode>absolute</altitudeMode><coordinates>
-1.0,51.0,30 -0.99,51.01,50
</coordinates></LineString></Placemark></kml>
"""
        for raw_path in paths:
            path = Path(raw_path)
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(valid_kml, encoding="utf-8")
        with (
            patch("pages.transpose_page.parse_kml_track", return_value=inferred_track()),
            patch("pages.transpose_page.infer_departure_runway", return_value=inferred_result()),
        ):
            self.page.add_files_to_list([str(path) for path in paths])

    def isolated_settings(self):
        self._settings_counter += 1
        return QSettings(
            str(self.root / f"settings-{self._settings_counter}.ini"),
            QSettings.Format.IniFormat,
        )

    def configure_target(self, runway="24"):
        values = AirfieldFormValues(
            airfield_name="RAF Fairford",
            runway=runway,
            threshold="51.0, -1.0",
            true_heading="240",
        )
        for state in self.page.source_states.values():
            state.runway_target_values = values
        if self.page._current_source_path is not None:
            self.page._render_source_state(
                self.page.source_states[self.page._current_source_path]
            )

    def test_two_column_workspace_and_filename_only_items(self):
        first = self.root / "one" / "display.kml"
        second = self.root / "two" / "display.kml"
        first.parent.mkdir()
        second.parent.mkdir()
        first.write_text("one", encoding="utf-8")
        second.write_text("two", encoding="utf-8")

        self.add_inferred_files(first, second, first)

        self.assertEqual(self.page.splitter.count(), 2)
        self.assertEqual(self.page.file_list.count(), 2)
        self.assertEqual(self.page.file_list.item(0).text(), "display.kml")
        self.assertEqual(self.page.file_list.item(1).text(), "display.kml")
        self.assertEqual(
            self.page.file_list.item(0).data(Qt.ItemDataRole.UserRole),
            str(first.resolve()),
        )
        self.assertEqual(self.page.file_list.item(0).toolTip(), str(first.resolve()))

    def test_original_and_target_airfields_are_side_by_side(self):
        self.page.resize(1100, 900)
        self.page.show()
        self.app.processEvents()

        source_bounds = self.page.source_card.geometry()
        target_bounds = self.page.target_card.geometry()
        self.assertLess(source_bounds.right(), target_bounds.left())
        self.assertFalse(source_bounds.intersects(target_bounds))

    def test_runway_original_airfield_shows_per_file_altitude_mode(self):
        first = self.root / "first.kml"
        second = self.root / "second.kml"
        self.add_inferred_files(first, second)

        altitude_output = self.page.source_card.altitude_mode_output
        altitude_label = self.page.source_card.altitude_mode_label
        elevation_label = self.page.source_card.elevation_label
        self.assertIsNotNone(altitude_output)
        self.assertIsNotNone(altitude_label)
        self.assertIsNotNone(elevation_label)
        self.assertTrue(altitude_output.isReadOnly())
        self.assertEqual(altitude_label.text(), "Altitude mode")
        self.assertEqual(altitude_output.text(), "Absolute")
        self.assertIsNone(self.page.target_card.altitude_mode_output)

        self.page.resize(1100, 900)
        self.page.show()
        self.app.processEvents()
        self.assertLess(
            altitude_label.geometry().bottom(),
            elevation_label.geometry().top(),
        )

        second_path = str(second.resolve())
        second_state = self.page.source_states[second_path]
        second_state.analysed = True
        second_state.altitude_mode = "relativeToGround"
        self.page.file_list.setCurrentRow(1)

        self.assertEqual(altitude_output.text(), "Relative to ground")

    def test_alignment_selector_sits_above_runway_and_manual_card_stacks(self):
        self.page.resize(1100, 900)
        self.page.show()
        self.app.processEvents()

        panel_bottom = self.page.alignment_method_panel.mapToGlobal(
            self.page.alignment_method_panel.rect().bottomLeft()
        ).y()
        stack_top = self.page.alignment_stack.mapToGlobal(
            self.page.alignment_stack.rect().topLeft()
        ).y()
        self.assertLess(panel_bottom, stack_top)
        self.assertTrue(self.page.runway_alignment_button.isChecked())
        self.assertFalse(self.page.manual_alignment_button.isChecked())
        self.assertEqual(self.page.alignment_stack.currentIndex(), 0)
        self.assertTrue(self.page.alignment_method_group.exclusive())
        self.assertIsInstance(self.page.runway_alignment_button, QRadioButton)
        self.assertIsInstance(self.page.manual_alignment_button, QRadioButton)
        self.assertEqual(
            self.page.runway_alignment_button.objectName(),
            "alignmentModeSegment",
        )
        self.assertEqual(
            self.page.manual_alignment_button.objectName(),
            "alignmentModeSegment",
        )
        self.assertTrue(
            self.page.alignment_mode_control.isAncestorOf(
                self.page.runway_alignment_button
            )
        )
        self.assertLessEqual(
            abs(
                self.page.runway_alignment_button.width()
                - self.page.manual_alignment_button.width()
            ),
            1,
        )
        panel_text = {
            label.text()
            for label in self.page.alignment_method_panel.findChildren(QLabel)
        }
        self.assertIn("Alignment mode", panel_text)
        self.assertNotIn("Alignment method", panel_text)
        self.assertIn(
            "QRadioButton#alignmentModeSegment:checked",
            self.page.styleSheet(),
        )

        self.page.manual_alignment_button.click()
        self.assertTrue(self.page.runway_alignment_button.isChecked())
        self.assertEqual(self.page.alignment_stack.currentIndex(), 0)

    def test_manual_alignment_uses_rotation_language(self):
        labels = [
            label.text()
            for label in self.page.target_trace_card.findChildren(QLabel)
        ]

        self.assertIn("Rotation (degrees)", labels)
        self.assertEqual(
            self.page.target_trace_card.rotation_input.placeholderText(),
            "0–360°",
        )
        self.assertEqual(
            self.page.target_trace_card.rotation_input.accessibleName(),
            "Target trace rotation in degrees",
        )
        user_copy = " ".join(labels).lower()
        self.assertNotIn("yaw", user_copy)
        self.assertNotIn("clockwise", user_copy)

    def test_manual_alignment_uses_first_file_point_and_preserves_per_file_drafts(self):
        first = self.root / "first.kml"
        second = self.root / "second.kml"
        self.add_inferred_files(first, second)
        first_path = str(first.resolve())

        self.page.manual_alignment_button.click()
        self.assertEqual(
            self.page.source_states[first_path].method,
            AlignmentMethod.MANUAL,
        )
        self.assertEqual(
            self.page.original_trace_card.coordinate_output.text(),
            "51, -1",
        )
        self.assertEqual(
            self.page.original_trace_card.altitude_output.text(),
            "30 m",
        )
        self.assertEqual(
            self.page.original_trace_card.altitude_mode_output.text(),
            "Absolute",
        )
        self.assertEqual(
            self.page.original_trace_card.altitude_mode_label.text(),
            "Altitude mode",
        )
        self.assertFalse(self.page.original_trace_card.ground_m_input.isHidden())
        self.page.target_trace_card.coordinate_input.setText("52.0, 0.25")
        self.page.target_trace_card.rotation_input.setText("35")
        self.page.original_trace_card.ground_m_input.setText("20")
        self.page._manual_form_edited()

        with (
            patch("pages.transpose_page.parse_kml_track", return_value=inferred_track()),
            patch("pages.transpose_page.infer_departure_runway", return_value=inferred_result()),
        ):
            self.page.file_list.setCurrentRow(1)
        self.page.manual_alignment_button.click()
        self.page.target_trace_card.coordinate_input.setText("53.0, 1.25")
        self.page._manual_form_edited()
        self.page.file_list.setCurrentRow(0)

        self.assertTrue(self.page.manual_alignment_button.isChecked())
        self.assertEqual(
            self.page.target_trace_card.coordinate_input.text(),
            "52.0, 0.25",
        )
        self.assertEqual(self.page.target_trace_card.rotation_input.text(), "35")
        self.assertEqual(self.page.original_trace_card.ground_m_input.text(), "20")

    def test_legacy_runway_preset_does_not_mutate_target_trace(self):
        source = self.root / "source.kml"
        self.add_inferred_files(source)
        record = self.page.preset_repository.create(
            "Farnborough",
            {
                "airfield_name": "Farnborough",
                "runway": "24",
                "threshold_latitude": 51.272833,
                "threshold_longitude": -0.792044,
                "true_heading_deg": 240.0,
                "elevation_m": 38.0,
            },
        )
        self.page._load_presets()
        self.page.manual_alignment_button.click()
        self.page.target_trace_card.rotation_input.setText("35")
        self.page._manual_form_edited()

        index = self.page.target_trace_card.preset_combo.findData(
            str(record.preset.id), Qt.ItemDataRole.UserRole
        )
        self.page.target_trace_card.preset_combo.setCurrentIndex(index)
        self.page.target_trace_card.preset_combo.activated.emit(index)

        state = self.page.source_states[str(source.resolve())]
        self.assertEqual(self.page.target_trace_card.coordinate_input.text(), "")
        self.assertEqual(state.manual_target_coordinate, "")
        self.assertEqual(self.page.target_trace_card.rotation_input.text(), "35")
        self.assertIn("does not contain Target trace", self.page.target_trace_card.error_label.text())

    def test_manual_target_preset_without_threshold_keeps_existing_coordinate(self):
        source = self.root / "source.kml"
        self.add_inferred_files(source)
        record = self.page.preset_repository.create(
            "Incomplete",
            {
                "airfield_name": "Incomplete",
                "runway": "24",
                "threshold_latitude": None,
                "threshold_longitude": None,
                "true_heading_deg": 240.0,
                "elevation_m": None,
            },
        )
        self.page._load_presets()
        self.page.manual_alignment_button.click()
        self.page.target_trace_card.coordinate_input.setText("52.0, 0.25")
        self.page._manual_form_edited()

        index = self.page.target_trace_card.preset_combo.findData(
            str(record.preset.id), Qt.ItemDataRole.UserRole
        )
        self.page.target_trace_card.preset_combo.setCurrentIndex(index)
        self.page.target_trace_card.preset_combo.activated.emit(index)

        self.assertEqual(self.page.target_trace_card.coordinate_input.text(), "52.0, 0.25")
        self.assertIn(
            "does not contain complete runway geometry",
            self.page.target_trace_card.error_label.text(),
        )

    def test_alignment_mode_radio_buttons_support_keyboard_selection(self):
        source = self.root / "source.kml"
        self.add_inferred_files(source)
        self.page.show()
        self.page.runway_alignment_button.setFocus()
        self.app.processEvents()

        QTest.keyClick(self.page.runway_alignment_button, Qt.Key.Key_Right)
        self.app.processEvents()

        self.assertTrue(self.page.manual_alignment_button.isChecked())
        self.assertEqual(self.page.alignment_stack.currentIndex(), 1)

    def test_manual_absolute_validation_requires_ground_and_normalises_rotation(self):
        source = self.root / "source.kml"
        self.add_inferred_files(source)
        self.page.manual_alignment_button.click()
        self.page.target_trace_card.coordinate_input.setText("52.0, 0.25")
        self.page.target_trace_card.rotation_input.setText("360")
        self.page._manual_form_edited()

        invalid = self.page._validated_inputs_for_paths((str(source.resolve()),))
        self.assertEqual(invalid, (None,))
        self.assertIn(
            "ground reference elevation is required",
            self.page.original_trace_card.error_label.text(),
        )

        self.page.original_trace_card.ground_m_input.setText("20")
        self.page._manual_form_edited()
        valid = self.page._validated_inputs_for_paths((str(source.resolve()),))
        self.assertIsInstance(valid[0], ManualTranspositionAlignment)
        self.assertEqual(valid[0].clockwise_rotation_deg, 0.0)

    def test_manual_relative_input_does_not_request_ground_elevation(self):
        source = self.root / "relative.kml"
        source.write_text("placeholder", encoding="utf-8")
        relative_track = KmlTrack(
            points=(
                KmlPoint(51.0, -1.0, 30.0),
                KmlPoint(51.01, -0.99, 50.0),
            ),
            geometry_kind="line_string",
            placemark_name="Display",
            altitude_mode="relativeToGround",
        )
        with (
            patch("pages.transpose_page.parse_kml_track", return_value=relative_track),
            patch("pages.transpose_page.infer_departure_runway", return_value=inferred_result()),
        ):
            self.page.add_files_to_list([str(source)])
        self.page.manual_alignment_button.click()
        self.page.target_trace_card.coordinate_input.setText("52.0, 0.25")
        self.page.target_trace_card.rotation_input.setText("0")
        self.page._manual_form_edited()

        alignment = self.page._validated_inputs_for_paths((str(source.resolve()),))[0]

        self.assertIsInstance(alignment, ManualTranspositionAlignment)
        self.assertIsNone(alignment.ground_reference_elevation_m)
        self.assertTrue(self.page.original_trace_card.ground_m_input.isHidden())
        self.assertEqual(
            self.page.original_trace_card.altitude_mode_output.text(),
            "Relative to ground",
        )

    def test_runway_targets_are_independent_per_file(self):
        first = self.root / "first.kml"
        second = self.root / "second.kml"
        self.add_inferred_files(first, second)
        self.configure_target(runway="24")
        first_state = self.page.source_states[str(first.resolve())]
        second_state = self.page.source_states[str(second.resolve())]
        first_state.runway_target_values = AirfieldFormValues(
            "First target", "24", "51, -1", "240"
        )
        second_state.runway_target_values = AirfieldFormValues(
            "Second target", "09", "52, 0", "90"
        )

        self.page._render_source_state(first_state)
        self.assertEqual(self.page.target_card.coordinate_input.text(), "51, -1")
        self.page.file_list.setCurrentRow(1)
        self.assertEqual(self.page.target_card.coordinate_input.text(), "52, 0")

    def test_alignment_drafts_restore_across_page_recreation(self):
        source = self.root / "source.kml"
        self.add_inferred_files(source)
        path = str(source.resolve())
        self.page.manual_alignment_button.click()
        self.page.target_trace_card.coordinate_input.setText("unfinished target")
        self.page.target_trace_card.rotation_input.setText("unfinished rotation")
        self.page.original_trace_card.ground_m_input.setText("71.4")
        self.page._manual_form_edited()
        self.page._schedule_profile_save(path, immediate=True)

        restored = TransposePage(settings=self.isolated_settings())
        try:
            with (
                patch("pages.transpose_page.parse_kml_track", return_value=inferred_track()),
                patch("pages.transpose_page.infer_departure_runway", return_value=inferred_result()),
            ):
                restored.add_files_to_list([path])
            state = restored.source_states[path]
            self.assertEqual(state.method, AlignmentMethod.MANUAL)
            self.assertEqual(state.manual_target_coordinate, "unfinished target")
            self.assertEqual(state.manual_rotation_deg, "unfinished rotation")
            self.assertEqual(state.manual_ground_elevation_m, "71.4")
        finally:
            restored.close()

    def test_runway_overrides_and_target_restore_and_restore_auto_clears_override(self):
        source = self.root / "source.kml"
        self.add_inferred_files(source)
        path = str(source.resolve())
        self.configure_target()
        self.page.source_card.coordinate_input.setText("51.25, -1.25")
        self.page._source_form_edited()
        self.page._commit_current_source()
        self.page._schedule_profile_save(path, immediate=True)

        restored = TransposePage(settings=self.isolated_settings())
        try:
            with (
                patch("pages.transpose_page.parse_kml_track", return_value=inferred_track()),
                patch("pages.transpose_page.infer_departure_runway", return_value=inferred_result()),
            ):
                restored.add_files_to_list([path])
            state = restored.source_states[path]
            self.assertTrue(state.source_overridden)
            self.assertEqual(state.values.threshold, "51.25, -1.25")
            self.assertEqual(state.runway_target_values.threshold, "51.0, -1.0")
            restored._restore_auto_source()
            self.assertFalse(state.source_overridden)
        finally:
            restored.close()

        final_page = TransposePage(settings=self.isolated_settings())
        try:
            with (
                patch("pages.transpose_page.parse_kml_track", return_value=inferred_track()),
                patch("pages.transpose_page.infer_departure_runway", return_value=inferred_result()),
            ):
                final_page.add_files_to_list([path])
            final_state = final_page.source_states[path]
            self.assertFalse(final_state.source_overridden)
            self.assertEqual(final_state.values.threshold, "51, -1")
        finally:
            final_page.close()

    def test_changed_kml_does_not_restore_saved_alignment(self):
        source = self.root / "source.kml"
        self.add_inferred_files(source)
        path = str(source.resolve())
        self.page.manual_alignment_button.click()
        self.page._schedule_profile_save(path, immediate=True)
        source.write_text(
            source.read_text(encoding="utf-8").replace("-0.99,51.01", "-0.98,51.02"),
            encoding="utf-8",
        )

        restored = TransposePage(settings=self.isolated_settings())
        try:
            with (
                patch("pages.transpose_page.parse_kml_track", return_value=inferred_track()),
                patch("pages.transpose_page.infer_departure_runway", return_value=inferred_result()),
            ):
                restored.add_files_to_list([path])
            state = restored.source_states[path]
            self.assertEqual(state.method, AlignmentMethod.RUNWAY)
            self.assertIn("has changed", state.persistence_notice)
        finally:
            restored.close()

    def test_preview_adjustment_restores_only_for_matching_alignment_signature(self):
        source = self.root / "source.kml"
        self.add_inferred_files(source)
        path = str(source.resolve())
        self.page.manual_alignment_button.click()
        state = self.page.source_states[path]
        self.page.target_trace_card.coordinate_input.setText("52.0, 0.25")
        self.page.target_trace_card.rotation_input.setText("35")
        self.page.original_trace_card.ground_m_input.setText("20")
        self.page._manual_form_edited()
        alignment = self.page._validated_inputs_for_paths((path,))[0]
        state.preview_signature = self.page._alignment_signature(alignment)
        state.preview_adjustment = TraceAdjustment(east_m=12.0, yaw_deg=4.0)
        state.preview_target_snapshot = PreviewTargetSnapshot(
            method=AlignmentMethod.MANUAL,
            coordinate="52.0, 0.25",
            clockwise_rotation="35",
        )
        self.page._schedule_profile_save(path, immediate=True)

        restored = TransposePage(settings=self.isolated_settings())
        try:
            with (
                patch("pages.transpose_page.parse_kml_track", return_value=inferred_track()),
                patch("pages.transpose_page.infer_departure_runway", return_value=inferred_result()),
            ):
                restored.add_files_to_list([path])
            restored_alignment = restored._validated_inputs_for_paths((path,))[0]
            self.assertEqual(
                restored.source_states[path].preview_target_snapshot,
                state.preview_target_snapshot,
            )
            batch = prepare_transposition(
                input_files=(source,),
                alignments=(restored_alignment,),
            )
            adjusted = restored._apply_committed_adjustments(
                batch,
                (restored_alignment,),
            )
            self.assertEqual(
                adjusted.prepared[0].trace.adjustment,
                TraceAdjustment(east_m=12.0, yaw_deg=4.0),
            )

            restored.target_trace_card.rotation_input.setText("36")
            restored._manual_form_edited()
            changed_alignment = restored._validated_inputs_for_paths((path,))[0]
            unchanged_batch = prepare_transposition(
                input_files=(source,),
                alignments=(changed_alignment,),
            )
            not_adjusted = restored._apply_committed_adjustments(
                unchanged_batch,
                (changed_alignment,),
            )
            self.assertTrue(not_adjusted.prepared[0].trace.adjustment.is_zero)
        finally:
            restored.close()

    def test_applied_preview_offsets_are_summarised_in_both_target_cards(self):
        source = self.root / "source.kml"
        self.add_inferred_files(source)
        self.configure_target()
        path = str(source.resolve())
        alignments = self.page._validated_inputs_for_paths((path,))
        batch = prepare_transposition(
            input_files=(source,),
            alignments=alignments,
        )
        adjustment = TraceAdjustment(
            east_m=12.25,
            north_m=-3.0,
            up_m=4.5,
            yaw_deg=6.75,
        )
        self.page._prepared_batch = batch
        self.page._prepared_alignments = alignments
        self.page._prepared_signature = self.page._preparation_signature(
            (path,),
            alignments,
        )

        self.page.accept_preview_scene(
            PreviewScene(
                (batch.prepared[0].trace.with_adjustment(adjustment),)
            )
        )

        expected = (
            "East +12.2 m · North -3.0 m · Height +4.5 m · "
            "Rotation +6.8°"
        )
        for summary in (
            self.page.runway_offset_summary,
            self.page.manual_offset_summary,
        ):
            self.assertEqual(summary.values_label.text(), expected)
            self.assertFalse(hasattr(summary, "status_label"))
            self.assertEqual(
                summary.status_dot.property("offsetState"),
                "active",
            )
            self.assertEqual(
                summary.status_dot.accessibleName(),
                "Preview Offset Active",
            )
            self.assertEqual(summary.status_dot.toolTip(), "")
            self.assertFalse(summary.status_tooltip_button.icon().isNull())
            self.assertEqual(
                summary.status_tooltip_button.accessibleName(),
                "Preview Offset Active details",
            )
            self.assertEqual(
                summary.status_tooltip_button.toolTip(),
                summary.status_tooltip_button.accessibleDescription(),
            )
            explanation = summary.status_tooltip_button.accessibleDescription()
            self.assertIn("currently active", explanation)
            self.assertNotIn("East", explanation)
            self.assertNotIn("Yaw", explanation)
            self.assertEqual(
                summary.status_tooltip_button.focusPolicy(),
                Qt.FocusPolicy.StrongFocus,
            )
            self.assertFalse(summary.restore_button.isHidden())
            self.assertTrue(summary.restore_button.isEnabled())
            self.assertFalse(summary.clear_button.isHidden())

    def test_offset_summary_tracks_selected_kml(self):
        first = self.root / "first.kml"
        second = self.root / "second.kml"
        self.add_inferred_files(first, second)
        self.configure_target()
        paths = (str(first.resolve()), str(second.resolve()))
        for path in paths:
            state = self.page.source_states[path]
            state.values = AirfieldFormValues(
                threshold="51.0, -1.0",
                true_heading="90",
                elevation_m="30",
            )
            state.altitude_mode = "absolute"
            state.analysed = True
        alignments = self.page._validated_inputs_for_paths(paths)
        adjustments = (
            TraceAdjustment(east_m=10.0),
            TraceAdjustment(north_m=20.0),
        )
        for path, alignment, adjustment in zip(
            paths,
            alignments,
            adjustments,
            strict=True,
        ):
            state = self.page.source_states[path]
            state.preview_signature = self.page._alignment_signature(alignment)
            state.preview_adjustment = adjustment

        self.page._render_source_state(self.page.source_states[paths[0]])
        self.assertIn("East +10.0 m", self.page.runway_offset_summary.values_label.text())
        self.page.file_list.setCurrentRow(1)
        self.assertIn("North +20.0 m", self.page.runway_offset_summary.values_label.text())
        self.assertNotIn("East +10.0 m", self.page.runway_offset_summary.values_label.text())

    def test_saved_offsets_become_inactive_and_reactivate_with_matching_alignment(self):
        source = self.root / "source.kml"
        self.add_inferred_files(source)
        self.configure_target()
        path = str(source.resolve())
        state = self.page.source_states[path]
        alignment = self.page._validated_inputs_for_paths((path,))[0]
        state.preview_signature = self.page._alignment_signature(alignment)
        state.preview_adjustment = TraceAdjustment(east_m=15.0, yaw_deg=2.0)
        state.preview_target_snapshot = PreviewTargetSnapshot(
            method=AlignmentMethod.RUNWAY,
            coordinate="51.0, -1.0",
            true_heading="240",
        )
        self.page._render_source_state(state)
        self.assertEqual(
            self.page.runway_offset_summary.status_dot.property("offsetState"),
            "active",
        )

        self.page.target_card.heading_input.setText("241")
        self.page._target_form_edited()
        self.assertEqual(
            self.page.runway_offset_summary.status_dot.property("offsetState"),
            "mismatch",
        )
        self.assertIn(
            "true heading",
            self.page.runway_offset_summary.status_tooltip_button
            .accessibleDescription(),
        )
        self.assertNotIn(
            "clockwise rotation",
            self.page.runway_offset_summary.status_tooltip_button
            .accessibleDescription(),
        )
        changed_alignment = self.page._validated_inputs_for_paths((path,))[0]
        changed_batch = prepare_transposition(
            input_files=(source,),
            alignments=(changed_alignment,),
        )
        changed_batch = self.page._apply_committed_adjustments(
            changed_batch,
            (changed_alignment,),
        )
        self.assertTrue(changed_batch.prepared[0].trace.adjustment.is_zero)

        self.page.target_card.heading_input.setText("240")
        self.page._target_form_edited()
        self.assertEqual(
            self.page.runway_offset_summary.status_dot.property("offsetState"),
            "active",
        )

    def test_restore_original_manual_target_switches_mode_and_keeps_rotation_distinct(self):
        source = self.root / "source.kml"
        self.add_inferred_files(source)
        path = str(source.resolve())
        self.page.manual_alignment_button.click()
        self.page.target_trace_card.coordinate_input.setText("52.0, 0.25")
        self.page.target_trace_card.rotation_input.setText("35")
        self.page.original_trace_card.ground_m_input.setText("20")
        self.page._manual_form_edited()
        state = self.page.source_states[path]
        alignment = self.page._validated_inputs_for_paths((path,))[0]
        state.preview_signature = self.page._alignment_signature(alignment)
        state.preview_adjustment = TraceAdjustment(north_m=8.0)
        state.preview_target_snapshot = PreviewTargetSnapshot(
            method=AlignmentMethod.MANUAL,
            coordinate="52.0, 0.25",
            clockwise_rotation="35",
        )

        self.page.target_trace_card.coordinate_input.setText("53.0, 1.25")
        self.page.target_trace_card.rotation_input.setText("40")
        self.page._manual_form_edited()
        self.assertIn(
            "rotation",
            self.page.manual_offset_summary.status_tooltip_button
            .accessibleDescription(),
        )
        self.assertNotIn(
            "clockwise",
            self.page.manual_offset_summary.status_tooltip_button
            .accessibleDescription().lower(),
        )
        self.assertNotIn(
            "true heading",
            self.page.manual_offset_summary.status_tooltip_button
            .accessibleDescription(),
        )
        self.page.runway_alignment_button.click()
        self.page.runway_offset_summary.restore_button.click()

        self.assertEqual(state.method, AlignmentMethod.MANUAL)
        self.assertTrue(self.page.manual_alignment_button.isChecked())
        self.assertEqual(state.manual_target_coordinate, "52.0, 0.25")
        self.assertEqual(state.manual_rotation_deg, "35")
        self.assertEqual(
            self.page.manual_offset_summary.status_dot.property("offsetState"),
            "active",
        )

    def test_restore_original_is_target_only_when_source_inputs_changed(self):
        source = self.root / "source.kml"
        self.add_inferred_files(source)
        self.configure_target()
        path = str(source.resolve())
        state = self.page.source_states[path]
        alignment = self.page._validated_inputs_for_paths((path,))[0]
        state.preview_signature = self.page._alignment_signature(alignment)
        state.preview_adjustment = TraceAdjustment(east_m=5.0)
        state.preview_target_snapshot = PreviewTargetSnapshot(
            method=AlignmentMethod.RUNWAY,
            coordinate="51.0, -1.0",
            true_heading="240",
        )
        state.values = replace(state.values, true_heading="91")
        state.runway_target_values = replace(
            state.runway_target_values,
            threshold="52.0, 0.5",
            true_heading="250",
        )
        self.page._render_source_state(state)

        self.page.runway_offset_summary.restore_button.click()

        self.assertEqual(state.values.true_heading, "91")
        self.assertEqual(state.runway_target_values.threshold, "51.0, -1.0")
        self.assertEqual(state.runway_target_values.true_heading, "240")
        self.assertEqual(
            self.page.runway_offset_summary.status_dot.property("offsetState"),
            "mismatch",
        )
        self.assertIn(
            "Source-side alignment inputs",
            self.page.runway_offset_summary.status_tooltip_button
            .accessibleDescription(),
        )

    def test_changed_source_file_marks_saved_offsets_inactive(self):
        source = self.root / "source.kml"
        self.add_inferred_files(source)
        self.configure_target()
        path = str(source.resolve())
        state = self.page.source_states[path]
        alignment = self.page._validated_inputs_for_paths((path,))[0]
        state.preview_signature = self.page._alignment_signature(alignment)
        state.preview_adjustment = TraceAdjustment(up_m=3.0)
        self.page._render_source_state(state)
        self.assertEqual(
            self.page.runway_offset_summary.status_dot.property("offsetState"),
            "active",
        )

        source.write_text(
            source.read_text(encoding="utf-8").replace(
                "-0.99,51.01,50",
                "-0.98,51.02,50",
            ),
            encoding="utf-8",
        )
        self.page._render_preview_offset_summaries(state)

        self.assertEqual(
            self.page.runway_offset_summary.status_dot.property("offsetState"),
            "mismatch",
        )
        self.assertIn(
            "source KML has changed",
            self.page.runway_offset_summary.status_tooltip_button
            .accessibleDescription(),
        )

    def test_legacy_offsets_keep_clear_available_but_disable_restore(self):
        source = self.root / "source.kml"
        self.add_inferred_files(source)
        self.configure_target()
        path = str(source.resolve())
        state = self.page.source_states[path]
        alignment = self.page._validated_inputs_for_paths((path,))[0]
        state.preview_signature = self.page._alignment_signature(alignment)
        state.preview_adjustment = TraceAdjustment(up_m=3.0)
        self.page._render_source_state(state)

        summary = self.page.runway_offset_summary
        self.assertFalse(summary.restore_button.isHidden())
        self.assertFalse(summary.restore_button.isEnabled())
        self.assertIn("older version", summary.restore_button.toolTip())
        self.assertFalse(summary.clear_button.isHidden())
        self.assertIn(
            "currently active",
            summary.status_tooltip_button.accessibleDescription(),
        )

    def test_clear_offsets_allows_fresh_preview_offsets_to_be_accepted(self):
        source = self.root / "source.kml"
        self.add_inferred_files(source)
        self.configure_target()
        path = str(source.resolve())
        state = self.page.source_states[path]
        alignment = self.page._validated_inputs_for_paths((path,))[0]
        state.preview_signature = self.page._alignment_signature(alignment)
        state.preview_adjustment = TraceAdjustment(east_m=5.0)
        state.preview_target_snapshot = PreviewTargetSnapshot(
            method=AlignmentMethod.RUNWAY,
            coordinate="51.0, -1.0",
            true_heading="240",
        )
        self.page._render_source_state(state)
        self.page.runway_offset_summary.clear_button.click()

        batch = prepare_transposition(
            input_files=(source,),
            alignments=(alignment,),
        )
        self.page._prepared_batch = batch
        self.page._prepared_alignments = (alignment,)
        self.page._prepared_signature = self.page._preparation_signature(
            (path,),
            (alignment,),
        )
        fresh_adjustment = TraceAdjustment(north_m=9.0, yaw_deg=1.0)
        self.page.accept_preview_scene(
            PreviewScene(
                (batch.prepared[0].trace.with_adjustment(fresh_adjustment),)
            )
        )

        self.assertEqual(state.preview_adjustment, fresh_adjustment)
        self.assertEqual(
            state.preview_target_snapshot,
            PreviewTargetSnapshot(
                method=AlignmentMethod.RUNWAY,
                coordinate="51, -1",
                true_heading="240",
            ),
        )
        self.assertEqual(
            self.page.runway_offset_summary.status_dot.property("offsetState"),
            "active",
        )

    def test_clear_offsets_affects_only_selected_kml_and_invalidates_cached_geometry(self):
        first = self.root / "first.kml"
        second = self.root / "second.kml"
        self.add_inferred_files(first, second)
        self.configure_target()
        paths = (str(first.resolve()), str(second.resolve()))
        for path in paths:
            state = self.page.source_states[path]
            state.values = AirfieldFormValues(
                threshold="51.0, -1.0",
                true_heading="90",
                elevation_m="30",
            )
            state.altitude_mode = "absolute"
            state.analysed = True
        alignments = self.page._validated_inputs_for_paths(paths)
        self.assertTrue(all(alignment is not None for alignment in alignments))
        for path, alignment, east_m in zip(
            paths,
            alignments,
            (10.0, 20.0),
            strict=True,
        ):
            state = self.page.source_states[path]
            state.preview_signature = self.page._alignment_signature(alignment)
            state.preview_adjustment = TraceAdjustment(east_m=east_m)
            state.preview_target_snapshot = PreviewTargetSnapshot(
                method=AlignmentMethod.RUNWAY,
                coordinate="51.0, -1.0",
                true_heading="240",
            )
            self.page._schedule_profile_save(path, immediate=True)

        saved_second_profile = self.page.alignment_profile_store.load(
            paths[1],
            self.page.source_states[paths[1]].fingerprint,
        ).profile
        self.assertIsNotNone(saved_second_profile)
        self.assertEqual(
            saved_second_profile.preview_adjustment,
            TraceAdjustment(east_m=20.0),
        )
        self.page._prepared_batch = object()
        self.page._prepared_signature = object()
        self.page._prepared_alignments = object()
        self.page._prepared_target_snapshots = object()
        self.page._accepted_signature = object()
        self.page._render_source_state(self.page.source_states[paths[0]])
        self.page.runway_offset_summary.clear_button.click()

        first_state = self.page.source_states[paths[0]]
        second_state = self.page.source_states[paths[1]]
        self.assertIsNone(first_state.preview_signature)
        self.assertIsNone(first_state.preview_adjustment)
        self.assertIsNone(first_state.preview_target_snapshot)
        self.assertEqual(second_state.preview_adjustment, TraceAdjustment(east_m=20.0))
        self.assertIsNone(self.page._prepared_batch)
        self.assertIsNone(self.page._prepared_signature)
        self.assertIsNone(self.page._prepared_alignments)
        self.assertIsNone(self.page._prepared_target_snapshots)
        self.assertIsNone(self.page._accepted_signature)
        self.assertEqual(
            self.page.runway_offset_summary.values_label.text(),
            "No preview offsets applied.",
        )
        self.assertTrue(self.page.runway_offset_summary.clear_button.isHidden())
        second_profile = self.page.alignment_profile_store.load(
            paths[1],
            second_state.fingerprint,
        ).profile
        self.assertIsNotNone(second_profile)
        self.assertEqual(
            second_profile.preview_adjustment,
            TraceAdjustment(east_m=20.0),
        )

        restored = TransposePage(settings=self.isolated_settings())
        try:
            with (
                patch("pages.transpose_page.parse_kml_track", return_value=inferred_track()),
                patch(
                    "pages.transpose_page.infer_departure_runway",
                    return_value=inferred_result(),
                ),
            ):
                restored.add_files_to_list(paths)
            self.assertIsNone(restored.source_states[paths[0]].preview_adjustment)
            self.assertEqual(
                restored.source_states[paths[1]].preview_adjustment,
                TraceAdjustment(east_m=20.0),
            )
        finally:
            restored.close()

    def test_input_panel_uses_compact_top_aligned_natural_height(self):
        self.page.file_list.addItems([f"display-{index}.kml" for index in range(6)])
        self.page.resize(1100, 900)
        self.page.show()
        self.app.processEvents()

        splitter_top = self.page.splitter.mapToGlobal(
            self.page.splitter.rect().topLeft()
        ).y()
        panel_top = self.page.file_panel.mapToGlobal(
            self.page.file_panel.rect().topLeft()
        ).y()
        self.assertEqual(panel_top, splitter_top)
        self.assertLess(self.page.file_panel.height(), self.page.splitter.height() * 0.6)
        self.assertGreaterEqual(
            self.page.file_list.height(),
            self.page.file_list.sizeHintForRow(0) * 5,
        )

    def test_each_target_card_hosts_connected_preview_and_transposition_buttons(self):
        self.page.show()
        self.app.processEvents()

        action_sets = (
            (
                self.page.target_card,
                self.page.runway_offset_summary,
                self.page.runway_preview_btn,
                self.page.runway_run_btn,
            ),
            (
                self.page.target_trace_card,
                self.page.manual_offset_summary,
                self.page.manual_preview_btn,
                self.page.manual_run_btn,
            ),
        )
        for index, (card, summary, preview_button, run_button) in enumerate(action_sets):
            with self.subTest(card=card):
                self.page.alignment_stack.setCurrentIndex(index)
                self.app.processEvents()
                self.assertTrue(card.isAncestorOf(summary))
                self.assertTrue(card.isAncestorOf(preview_button))
                self.assertTrue(card.isAncestorOf(run_button))
                self.assertLess(preview_button.x(), run_button.x())
                self.assertEqual(preview_button.text(), "View preview")
                self.assertFalse(preview_button.icon().isNull())
                self.assertEqual(run_button.text(), "Transpose files")
                self.assertEqual(run_button.objectName(), "primaryButton")
                self.assertTrue(preview_button.isEnabled())
                self.assertEqual(preview_button.receivers(preview_button.clicked), 1)
                self.assertEqual(run_button.receivers(run_button.clicked), 1)

        self.assertIs(self.page.preview_btn, self.page.runway_preview_btn)
        self.assertIs(self.page.run_btn, self.page.runway_run_btn)

    def test_offset_summaries_cover_no_file_and_no_adjustment_states(self):
        for summary in (
            self.page.runway_offset_summary,
            self.page.manual_offset_summary,
        ):
            self.assertEqual(
                summary.values_label.text(),
                "Select a KML file to review preview offsets.",
            )
            self.assertTrue(summary.clear_button.isHidden())
            self.assertTrue(summary.restore_button.isHidden())
            self.assertEqual(summary.status_dot.property("offsetState"), "none")
            self.assertEqual(summary.status_dot.toolTip(), "")
            self.assertEqual(
                summary.status_tooltip_button.accessibleName(),
                "No Preview Offset details",
            )
            self.assertIn(
                "no preview offset is currently applied",
                summary.status_tooltip_button.accessibleDescription().casefold(),
            )
            self.assertFalse(summary.status_tooltip_button.isHidden())

        source = self.root / "source.kml"
        self.add_inferred_files(source)
        for summary in (
            self.page.runway_offset_summary,
            self.page.manual_offset_summary,
        ):
            self.assertEqual(
                summary.values_label.text(),
                "No preview offsets applied.",
            )
            self.assertTrue(summary.clear_button.isHidden())

        state = self.page.source_states[str(source.resolve())]
        state.preview_adjustment = TraceAdjustment()
        self.page._render_preview_offset_summaries(state)
        self.assertEqual(
            self.page.runway_offset_summary.values_label.text(),
            "No preview offsets applied.",
        )
        self.assertTrue(self.page.runway_offset_summary.clear_button.isHidden())

    def test_offset_status_tooltip_supports_hover_and_click(self):
        self.page.show()
        self.app.processEvents()
        for summary in (
            self.page.runway_offset_summary,
            self.page.manual_offset_summary,
        ):
            button = summary.status_tooltip_button
            expected = button.accessibleDescription()
            self.assertEqual(button.toolTip(), expected)

            button.click()
            self.app.processEvents()
            popup = summary.status_popup.popup
            self.assertIsNotNone(popup)
            self.assertTrue(popup.isVisible())
            self.assertEqual(
                popup.findChild(QLabel, "persistentInfoPopupText").text(),
                expected,
            )
            self.app.processEvents()
            self.assertTrue(popup.isVisible())

            button.click()
            self.app.processEvents()
            self.assertIsNone(summary.status_popup.popup)

    def test_persistent_information_popup_dismissal_and_stale_state(self):
        self.page.show()
        self.app.processEvents()
        summary = self.page.runway_offset_summary
        button = summary.status_tooltip_button

        button.setFocus()
        QTest.keyClick(button, Qt.Key.Key_Space)
        self.app.processEvents()
        popup = summary.status_popup.popup
        self.assertIsNotNone(popup)
        QTest.keyClick(popup, Qt.Key.Key_Escape)
        self.app.processEvents()
        self.assertIsNone(summary.status_popup.popup)
        self.assertTrue(button.hasFocus())

        button.click()
        self.app.processEvents()
        self.assertIsNotNone(summary.status_popup.popup)
        QTest.mouseClick(self.page.file_count_label, Qt.MouseButton.LeftButton)
        self.app.processEvents()
        self.assertIsNone(summary.status_popup.popup)

        source = self.root / "source.kml"
        self.add_inferred_files(source)
        button.click()
        self.app.processEvents()
        self.assertIsNotNone(summary.status_popup.popup)
        self.page.manual_alignment_button.click()
        self.app.processEvents()
        self.assertIsNone(summary.status_popup.popup)

    def test_target_actions_fit_inside_both_cards_at_supported_sizes(self):
        self.page.show()
        action_sets = (
            (
                self.page.target_card,
                self.page.runway_preview_btn,
                self.page.runway_run_btn,
            ),
            (
                self.page.target_trace_card,
                self.page.manual_preview_btn,
                self.page.manual_run_btn,
            ),
        )

        for width, height in ((900, 600), (1100, 900)):
            self.page.resize(width, height)
            for index, (card, preview_button, run_button) in enumerate(action_sets):
                with self.subTest(size=(width, height), card=card):
                    self.page.alignment_stack.setCurrentIndex(index)
                    self.app.processEvents()
                    for button in (preview_button, run_button):
                        self.assertTrue(card.rect().contains(button.geometry()))
                        self.assertNotEqual(
                            button.focusPolicy(),
                            Qt.FocusPolicy.NoFocus,
                        )

    def test_original_trace_coordinate_field_is_not_narrower_than_target(self):
        self.page.show()
        self.page.alignment_stack.setCurrentIndex(1)

        for width, height in ((900, 600), (1100, 900)):
            with self.subTest(size=(width, height)):
                self.page.resize(width, height)
                self.app.processEvents()
                self.assertGreaterEqual(
                    self.page.original_trace_card.coordinate_output.width(),
                    self.page.target_trace_card.coordinate_input.width(),
                )

    def test_preset_controls_share_one_row_in_all_alignment_cards(self):
        self.page.show()
        cards_by_page = (
            (self.page.source_card, self.page.target_card),
            (self.page.original_trace_card, self.page.target_trace_card),
        )

        for index, cards in enumerate(cards_by_page):
            self.page.alignment_stack.setCurrentIndex(index)
            self.app.processEvents()
            for card in cards:
                with self.subTest(card=card):
                    combo_rect = card.preset_combo.geometry()
                    button_rect = card.save_preset_btn.geometry()
                    self.assertLess(combo_rect.x(), button_rect.x())
                    self.assertLess(
                        max(combo_rect.top(), button_rect.top()),
                        min(combo_rect.bottom(), button_rect.bottom()),
                    )

    def test_runway_coordinate_labels_and_manual_source_row_layout(self):
        self.page.show()

        self.page.alignment_stack.setCurrentIndex(0)
        self.app.processEvents()
        for card in (self.page.source_card, self.page.target_card):
            with self.subTest(card=card):
                matching_labels = [
                    label
                    for label in card.findChildren(QLabel)
                    if label.text() == "Runway coordinates"
                ]
                self.assertEqual(len(matching_labels), 1)

        self.page.alignment_stack.setCurrentIndex(1)
        self.app.processEvents()
        source_label = next(
            label
            for label in self.page.original_trace_card.findChildren(QLabel)
            if label.text() == "Source coordinates"
        )
        label_rect = source_label.geometry()
        field_rect = self.page.original_trace_card.coordinate_output.geometry()
        self.assertLess(label_rect.x(), field_rect.x())
        self.assertLess(
            max(label_rect.top(), field_rect.top()),
            min(label_rect.bottom(), field_rect.bottom()),
        )

    def test_transposition_selection_dialog_supports_all_none_and_requires_a_choice(self):
        first = self.root / "first.kml"
        second = self.root / "second.kml"
        dialog = TranspositionInputDialog(
            (str(first), str(second)),
            (str(first),),
            self.page,
        )

        self.assertEqual(dialog._checked_paths(), (str(first.resolve()),))
        dialog.select_none_button.click()
        self.assertFalse(dialog.continue_button.isEnabled())
        dialog._validate_and_accept()
        self.assertIn("at least one", dialog.error_label.text())

        dialog.select_all_button.click()
        self.assertTrue(dialog.continue_button.isEnabled())
        dialog._validate_and_accept()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(
            dialog.selected_paths,
            (str(first.resolve()), str(second.resolve())),
        )
        dialog.close()

    def test_transposition_selection_defaults_to_current_then_remembers_submission(self):
        first = self.root / "first.kml"
        second = self.root / "second.kml"
        self.add_inferred_files(first, second)
        first_path = str(first.resolve())
        second_path = str(second.resolve())

        with patch("pages.transpose_page.TranspositionInputDialog") as dialog_class:
            dialog = dialog_class.return_value
            dialog.exec.return_value = QDialog.DialogCode.Accepted
            dialog.selected_paths = (second_path,)
            self.assertEqual(self.page._choose_transposition_inputs(), (second_path,))
            self.assertEqual(dialog_class.call_args.args[1], (first_path,))

        with patch("pages.transpose_page.TranspositionInputDialog") as dialog_class:
            dialog = dialog_class.return_value
            dialog.exec.return_value = QDialog.DialogCode.Rejected
            dialog.selected_paths = (first_path,)
            self.assertIsNone(self.page._choose_transposition_inputs())
            self.assertEqual(dialog_class.call_args.args[1], (second_path,))

        self.assertEqual(self.page._last_transposition_selection, (second_path,))

        self.page.file_list.clearSelection()
        self.page.file_list.item(1).setSelected(True)
        self.page.remove_selected_files()
        self.assertEqual(self.page._last_transposition_selection, ())

        with patch("pages.transpose_page.TranspositionInputDialog") as dialog_class:
            dialog_class.return_value.exec.return_value = QDialog.DialogCode.Rejected
            self.assertIsNone(self.page._choose_transposition_inputs())
            self.assertEqual(dialog_class.call_args.args[1], (first_path,))

    def test_preview_prepares_all_ready_files_with_current_file_first(self):
        first = self.root / "first.kml"
        second = self.root / "second.kml"
        self.add_inferred_files(first, second)
        self.configure_target()
        second_state = self.page.source_states[str(second.resolve())]
        second_state.analysed = True
        second_state.altitude_mode = "absolute"
        second_state.values = AirfieldFormValues(
            threshold="51.0, -1.0",
            true_heading="90",
            elevation_m="30",
        )
        self.page.file_list.setCurrentRow(1)
        scenes = []
        self.page.preview_requested.connect(scenes.append)

        with (
            patch(
                "pages.transpose_page.prepare_transposition",
                wraps=prepare_transposition,
            ) as prepare,
            patch.object(QMessageBox, "warning") as warning,
        ):
            self.page.open_preview()

        self.assertEqual(
            prepare.call_args.kwargs["input_files"],
            (str(first.resolve()), str(second.resolve())),
        )
        self.assertEqual(len(scenes), 1)
        self.assertEqual(len(scenes[0].traces), 2)
        self.assertEqual(scenes[0].traces[0].label, "second")
        self.assertEqual(scenes[0].traces[1].label, "first")
        self.assertEqual(self.page._prepared_batch.prepared_count, 2)
        self.assertEqual(
            tuple(
                str(item.input_path.resolve())
                for item in self.page._prepared_batch.prepared
            ),
            (str(first.resolve()), str(second.resolve())),
        )
        warning.assert_not_called()

        with patch.object(self.page, "_run_transposition_for_paths") as run:
            self.page.export_committed_scene()

        run.assert_called_once_with((str(first.resolve()), str(second.resolve())))

    def test_preview_skips_invalid_files_and_export_keeps_only_ready_files(self):
        first = self.root / "first.kml"
        incomplete = self.root / "incomplete.kml"
        self.add_inferred_files(first, incomplete)
        self.configure_target()
        incomplete_state = self.page.source_states[str(incomplete.resolve())]
        incomplete_state.analysed = True
        incomplete_state.altitude_mode = "absolute"
        incomplete_state.values = AirfieldFormValues(
            threshold="51.0, -1.0",
            true_heading="90",
            elevation_m="30",
        )
        incomplete_state.runway_target_values = AirfieldFormValues()
        scenes = []
        self.page.preview_requested.connect(scenes.append)

        with patch.object(QMessageBox, "warning") as warning:
            self.page.open_preview()

        self.assertEqual(len(scenes), 1)
        self.assertEqual(len(scenes[0].traces), 1)
        self.assertEqual(scenes[0].traces[0].label, "first")
        self.assertEqual(self.page._prepared_batch.prepared_count, 1)
        self.assertEqual(self.page._prepared_batch.failure_count, 0)
        warning.assert_called_once()
        self.assertEqual(
            warning.call_args.args[1],
            "Some KML files are not ready",
        )
        warning_text = warning.call_args.args[2]
        self.assertIn(
            "Complete the highlighted transposition details for these KML files "
            "before they can be visualised.",
            warning_text,
        )
        self.assertIn("incomplete.kml", warning_text)
        self.assertIn("target airfield departure threshold", warning_text)
        self.assertIn("Enter a latitude and longitude", warning_text)
        self.assertIn(
            "target airfield departure threshold",
            incomplete_state.target_error,
        )

        with patch.object(self.page, "_run_transposition_for_paths") as run:
            self.page.export_committed_scene()

        run.assert_called_once_with((str(first.resolve()),))

    def test_preview_does_not_open_when_no_file_is_ready(self):
        first = self.root / "first.kml"
        second = self.root / "second.kml"
        self.add_inferred_files(first, second)
        scenes = []
        self.page.preview_requested.connect(scenes.append)

        with (
            patch("pages.transpose_page.prepare_transposition") as prepare,
            patch.object(QMessageBox, "warning") as warning,
        ):
            self.page.open_preview()

        prepare.assert_not_called()
        self.assertFalse(scenes)
        warning.assert_called_once()
        warning_text = warning.call_args.args[2]
        self.assertIn("first.kml", warning_text)
        self.assertIn("second.kml", warning_text)
        self.assertIn("before they can be visualised", warning_text)

    def test_preview_export_uses_its_file_without_opening_selection_dialog(self):
        source = self.root / "source.kml"
        self.add_inferred_files(source)
        self.page._prepared_batch = prepare_transposition(
            input_files=(source,),
            source_runways=(RunwayReference(51.0, -1.0, 90.0, 30.0),),
            target_runway=RunwayReference(51.1, -0.8, 120.0),
        )

        with (
            patch.object(self.page, "_choose_transposition_inputs") as choose,
            patch.object(self.page, "_run_transposition_for_paths") as run,
        ):
            self.page.export_committed_scene()

        choose.assert_not_called()
        run.assert_called_once_with((str(source.resolve()),))

    def test_unselected_invalid_file_does_not_affect_transposition_preflight(self):
        first = self.root / "first.kml"
        unrelated = self.root / "broken.kml"
        self.add_inferred_files(first, unrelated)
        unrelated_state = self.page.source_states[str(unrelated.resolve())]
        unrelated_state.analysed = True
        unrelated_state.parse_error = "Unrelated file is broken."
        self.page._sync_file_item_error(unrelated)
        self.configure_target()

        with (
            patch.object(
                self.page,
                "_choose_transposition_inputs",
                return_value=(str(first.resolve()),),
            ),
            patch(
                "pages.transpose_page.prepare_transposition",
                wraps=prepare_transposition,
            ) as prepare,
            patch.object(QFileDialog, "getExistingDirectory", return_value="") as folder,
            patch.object(QMessageBox, "warning") as warning,
        ):
            self.page.run_transposition_ui()

        self.assertEqual(
            prepare.call_args.kwargs["input_files"],
            (str(first.resolve()),),
        )
        folder.assert_called_once()
        warning.assert_not_called()

    def test_selected_errors_are_marked_summarised_and_opened_inline(self):
        first = self.root / "first.kml"
        second = self.root / "second.kml"
        self.add_inferred_files(first, second)
        self.configure_target()

        self.page.source_card.coordinate_input.clear()
        self.page._source_form_edited()
        with (
            patch("pages.transpose_page.parse_kml_track", return_value=inferred_track()),
            patch(
                "pages.transpose_page.infer_departure_runway",
                return_value=inferred_result(),
            ),
        ):
            self.page.file_list.setCurrentRow(1)
        self.page.source_card.elevation_m_input.clear()
        self.page._source_form_edited()

        with (
            patch.object(
                self.page,
                "_choose_transposition_inputs",
                return_value=(str(first.resolve()), str(second.resolve())),
            ),
            patch.object(QFileDialog, "getExistingDirectory") as folder,
            patch.object(QMessageBox, "warning") as warning,
        ):
            self.page.run_transposition_ui()

        folder.assert_not_called()
        warning.assert_called_once()
        message = warning.call_args.args[2]
        self.assertIn("first.kml", message)
        self.assertIn("departure threshold", message)
        self.assertIn("second.kml", message)
        self.assertIn("elevation is required", message)
        self.assertEqual(self.page._current_source_path, str(first.resolve()))
        self.assertFalse(self.page.file_list.item(0).icon().isNull())
        self.assertFalse(self.page.file_list.item(1).icon().isNull())
        self.assertIn("departure threshold", self.page.source_card.error_label.text())
        self.assertTrue(self.page.source_card.elevation_m_input.isEnabled())

        self.page.file_list.setCurrentRow(1)
        self.assertIn("elevation is required", self.page.source_card.error_label.text())
        self.page.source_card.elevation_m_input.setText("30")
        self.page._source_form_edited()
        self.assertTrue(self.page.file_list.item(1).icon().isNull())
        self.assertFalse(self.page.file_list.item(0).icon().isNull())

    def test_action_buttons_use_local_icons(self):
        for button in (
            self.page.add_files_btn,
            self.page.remove_files_btn,
            self.page.manage_airfields_btn,
            self.page.preview_btn,
            self.page.manual_preview_btn,
            self.page.source_card.details_button,
        ):
            with self.subTest(button=button.text()):
                self.assertFalse(button.icon().isNull())

    def test_add_files_uses_and_remembers_transposition_input_directory(self):
        source = self.root / "source.kml"
        initial = str(self.root / "remembered-input")
        with (
            patch("pages.transpose_page.remembered_directory", return_value=initial) as remembered,
            patch.object(QFileDialog, "getOpenFileNames", return_value=([str(source)], "")) as chooser,
            patch("pages.transpose_page.remember_file_selection") as remember,
            patch("pages.transpose_page.parse_kml_track", return_value=inferred_track()),
            patch("pages.transpose_page.infer_departure_runway", return_value=inferred_result()),
        ):
            self.page.browse_files()

        remembered.assert_called_once_with(
            FileDialogWorkflow.TRANSPOSITION,
            FileDialogDirection.INPUT,
        )
        chooser.assert_called_once_with(
            self.page,
            "Select KML Files",
            initial,
            "KML Files (*.kml)",
        )
        remember.assert_called_once_with(
            FileDialogWorkflow.TRANSPOSITION,
            FileDialogDirection.INPUT,
            str(source),
        )
        self.assertEqual(self.page.file_list.item(0).text(), "source.kml")

    def test_selected_file_preserves_manual_state_and_restores_auto_values(self):
        first = self.root / "first.kml"
        second = self.root / "second.kml"
        self.add_inferred_files(first, second)
        self.assertEqual(self.page.source_card.status_label.text(), "Auto-detected")
        self.assertTrue(self.page.source_card.details_button.isEnabled())
        self.assertEqual(self.page.source_card.details_button.text(), "")
        self.assertFalse(self.page.source_card.details_button.icon().isNull())
        self.assertIn(
            "High Confidence — 86%",
            self.page.source_card.details_button.toolTip(),
        )
        self.assertIn(
            "Weakest detection signal",
            self.page.source_card.details_button.toolTip(),
        )
        self.assertEqual(
            self.page.source_card.detection_status_dot.property("detectionState"),
            "high",
        )
        self.assertIn(
            "86 percent",
            self.page.source_card.detection_status_dot.accessibleName(),
        )
        self.assertFalse(hasattr(self.page.source_card, "confidence_label"))
        self.page.show()
        self.app.processEvents()
        self.page.source_card.details_button.click()
        self.app.processEvents()
        popup = self.page.source_card.details_popup.popup
        self.assertIsNotNone(popup)
        self.assertTrue(popup.isVisible())
        self.assertEqual(
            popup.findChild(QLabel, "persistentInfoPopupText").text(),
            self.page.source_card.details_button.toolTip(),
        )
        self.app.processEvents()
        self.assertTrue(popup.isVisible())

        self.page.source_card.coordinate_input.setText("51.25, -1.25")
        self.page._source_form_edited()
        self.app.processEvents()
        self.assertIsNone(self.page.source_card.details_popup.popup)
        first_state = self.page.source_states[str(first.resolve())]
        self.assertEqual(first_state.provenance, "Manual override")
        self.assertEqual(
            self.page.source_card.detection_status_dot.property("detectionState"),
            "inactive",
        )
        self.assertTrue(self.page.source_card.details_button.isHidden())
        self.assertFalse(self.page.source_card.restore_button.isHidden())

        with (
            patch("pages.transpose_page.parse_kml_track", return_value=inferred_track()),
            patch("pages.transpose_page.infer_departure_runway", return_value=inferred_result()),
        ):
            self.page.file_list.setCurrentRow(1)
        self.page.file_list.setCurrentRow(0)
        self.assertEqual(self.page.source_card.coordinate_input.text(), "51.25, -1.25")
        self.assertFalse(self.page.source_card.restore_button.isHidden())

        self.page._restore_auto_source()
        self.assertEqual(self.page.source_card.coordinate_input.text(), "51, -1")
        self.assertEqual(self.page.source_card.status_label.text(), "Auto-detected")
        self.assertEqual(
            self.page.source_card.detection_status_dot.property("detectionState"),
            "high",
        )
        self.assertFalse(self.page.source_card.details_button.isHidden())
        self.assertTrue(self.page.source_card.restore_button.isHidden())

    def test_detection_indicator_covers_moderate_low_and_unavailable_states(self):
        source = self.root / "source.kml"
        self.add_inferred_files(source)
        state = self.page.source_states[str(source.resolve())]
        base_candidate = inferred_result().candidate
        base_assessment = base_candidate.detection_assessment

        for rating, percent, expected_state, wording in (
            (RunwayConfidence.MEDIUM, 72, "moderate", "Moderate Confidence"),
            (RunwayConfidence.LOW, 45, "low", "Low Confidence"),
        ):
            with self.subTest(rating=rating):
                assessment = replace(
                    base_assessment,
                    rating=rating,
                    overall_percent=percent,
                )
                state.inference = RunwayInferenceResult(
                    candidate=replace(
                        base_candidate,
                        detection_assessment=assessment,
                    )
                )
                state.provenance = "Auto-detected"
                state.details = "Evidence:\nSynthetic evidence"
                self.page._render_source_status(state)

                self.assertEqual(
                    self.page.source_card.detection_status_dot.property(
                        "detectionState"
                    ),
                    expected_state,
                )
                self.assertIn(wording, self.page.source_card.details_button.toolTip())
                self.assertNotIn(wording, self.page.source_card.status_label.text())

        state.inference = RunwayInferenceResult(
            candidate=None,
            error="No runway candidate was detected.",
        )
        state.provenance = "Needs input"
        state.details = "No runway candidate was detected."
        self.page._render_source_status(state)

        self.assertTrue(self.page.source_card.detection_status_dot.isHidden())
        self.assertFalse(self.page.source_card.details_button.isHidden())
        self.assertEqual(
            self.page.source_card.details_button.toolTip(),
            "No runway candidate was detected.",
        )

    def test_materially_replaced_source_does_not_restore_its_committed_adjustment(self):
        source = self.root / "source.kml"
        self.add_inferred_files(source)
        source_runway = RunwayReference(51.0, -1.0, 90.0, 30.0)
        target_runway = RunwayReference(51.1, -0.8, 120.0)
        batch = prepare_transposition(
            input_files=(source,),
            source_runways=(source_runway,),
            target_runway=target_runway,
        )
        adjustment = TraceAdjustment(east_m=20.0)
        alignment = RunwayTranspositionAlignment(source_runway, target_runway)
        state = self.page.source_states[str(source.resolve())]
        state.fingerprint = self.page._source_fingerprint(source)
        state.preview_signature = self.page._alignment_signature(alignment)
        state.preview_adjustment = adjustment
        adjusted = self.page._apply_committed_adjustments(
            batch,
            (alignment,),
        )
        self.assertEqual(adjusted.prepared[0].trace.adjustment, adjustment)

        source.write_text(
            source.read_text(encoding="utf-8").replace("-0.99,51.01", "-0.98,51.02"),
            encoding="utf-8",
        )
        replacement = prepare_transposition(
            input_files=(source,),
            source_runways=(source_runway,),
            target_runway=target_runway,
        )
        reapplied = self.page._apply_committed_adjustments(
            replacement,
            (alignment,),
        )

        self.assertTrue(reapplied.prepared[0].trace.adjustment.is_zero)

    def test_source_and_target_preset_application_have_different_elevation_semantics(self):
        source = self.root / "source.kml"
        self.add_inferred_files(source)
        record = self.page.preset_repository.create(
            "Farnborough — RWY 24",
            {
                "airfield_name": "Farnborough",
                "runway": "24",
                "threshold_latitude": 51.272833,
                "threshold_longitude": -0.792044,
                "true_heading_deg": 240.0,
                "elevation_m": 38.0,
            },
        )
        self.page._load_presets()

        for card in (self.page.source_card, self.page.target_card):
            index = card.preset_combo.findData(
                str(record.preset.id),
                Qt.ItemDataRole.UserRole,
            )
            card.preset_combo.setCurrentIndex(index)
            card.preset_combo.activated.emit(index)

        self.assertEqual(self.page.source_card.elevation_m_input.text(), "38")
        self.assertEqual(self.page.source_card.status_label.text(), "Preset")
        self.assertEqual(
            self.page.source_card.detection_status_dot.property("detectionState"),
            "inactive",
        )
        self.assertTrue(self.page.source_card.details_button.isHidden())
        self.assertFalse(self.page.source_card.restore_button.isHidden())
        self.assertEqual(
            self.page.target_card.coordinate_input.text(),
            "51.272833, -0.792044",
        )
        self.assertFalse(self.page.target_card.include_elevation)
        self.assertFalse(
            any(
                button.text() == "Apply"
                for card in (self.page.source_card, self.page.target_card)
                for button in card.findChildren(QPushButton)
            )
        )

    def test_runway_cards_use_preset_identity_without_name_or_runway_fields(self):
        for card in (self.page.source_card, self.page.target_card):
            self.assertEqual(card.preset_label.text(), "Preset name")
            self.assertFalse(hasattr(card, "name_input"))
            self.assertFalse(hasattr(card, "runway_input"))
            self.assertEqual(card.save_preset_btn.text(), "Save preset")

        for card in (self.page.original_trace_card, self.page.target_trace_card):
            self.assertEqual(card.preset_label.text(), "Preset name")
            self.assertEqual(card.save_preset_btn.text(), "Save preset")

    def test_four_card_saves_merge_sections_and_preserve_runway_elevation(self):
        source = self.root / "source.kml"
        self.add_inferred_files(source)
        state = self.page.source_states[str(source.resolve())]

        with patch.object(
            QInputDialog,
            "getText",
            return_value=("Shared alignment", True),
        ):
            self.page._save_card_preset("sourceRunway")
        preset_id = state.source_runway_preset_id
        self.assertIsNotNone(preset_id)

        self.page.target_card.coordinate_input.setText("52.0, 0.25")
        self.page.target_card.heading_input.setText("120")
        for role, card in (
            ("targetRunway", self.page.target_card),
            ("originalTrace", self.page.original_trace_card),
            ("targetTrace", self.page.target_trace_card),
        ):
            index = card.preset_combo.findData(
                preset_id, Qt.ItemDataRole.UserRole
            )
            card.preset_combo.setCurrentIndex(index)
            if role == "originalTrace":
                self.page.original_trace_card.ground_m_input.setText("21.5")
            elif role == "targetTrace":
                self.page.target_trace_card.coordinate_input.setText("53.0, 1.25")
                self.page.target_trace_card.rotation_input.setText("35")
            with patch.object(
                QMessageBox,
                "question",
                return_value=QMessageBox.StandardButton.Yes,
            ):
                self.page._save_card_preset(role)

        record = self.page.presets[UUID(preset_id)]
        payload, _ = TranspositionPresetData.from_mapping(record.preset.data)
        self.assertEqual(payload.runway.elevation_m, 30.0)
        self.assertEqual(payload.runway.threshold_latitude, 52.0)
        self.assertEqual(payload.original_trace.ground_elevation_m, 21.5)
        self.assertEqual(payload.target_trace.rotation_deg, 35.0)
        self.assertEqual(state.target_runway_preset_id, preset_id)
        self.assertEqual(state.original_trace_preset_id, preset_id)
        self.assertEqual(state.target_trace_preset_id, preset_id)

        restored = TransposePage(settings=self.isolated_settings())
        try:
            with (
                patch("pages.transpose_page.parse_kml_track", return_value=inferred_track()),
                patch(
                    "pages.transpose_page.infer_departure_runway",
                    return_value=inferred_result(),
                ),
            ):
                restored.add_files_to_list([str(source)])
            restored_state = restored.source_states[str(source.resolve())]
            self.assertEqual(restored_state.source_runway_preset_id, preset_id)
            self.assertEqual(restored_state.target_runway_preset_id, preset_id)
            self.assertEqual(restored_state.original_trace_preset_id, preset_id)
            self.assertEqual(restored_state.target_trace_preset_id, preset_id)
        finally:
            restored.close()

    def test_cancelled_selected_preset_update_keeps_stored_data(self):
        source = self.root / "source.kml"
        self.add_inferred_files(source)
        with patch.object(
            QInputDialog,
            "getText",
            return_value=("Existing", True),
        ):
            self.page._save_card_preset("sourceRunway")
        state = self.page.source_states[str(source.resolve())]
        preset_id = state.source_runway_preset_id
        before = self.page.presets[UUID(preset_id)].preset.data
        self.page.source_card.heading_input.setText("180")
        self.page._source_form_edited()

        with patch.object(
            QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.Cancel,
        ):
            self.page._save_card_preset("sourceRunway")

        after = self.page.preset_repository.load_all()[UUID(preset_id)].preset.data
        self.assertEqual(after, before)

    def test_loaded_input_files_and_current_selection_restore_in_order(self):
        first = self.root / "first.kml"
        second = self.root / "second.kml"
        self.add_inferred_files(first, second)
        self.page.file_list.setCurrentRow(1)

        restored = TransposePage(settings=self.settings)
        try:
            self.assertEqual(
                restored.input_files,
                [str(first.resolve()), str(second.resolve())],
            )
            self.assertEqual(restored._current_source_path, str(second.resolve()))
        finally:
            restored.close()

    def test_restored_missing_file_remains_visible_with_error(self):
        missing = self.root / "missing.kml"
        settings = self.isolated_settings()
        settings.setValue(
            "transpose/input-files",
            [str(missing), "not-a-kml.txt", 42, str(missing)],
        )
        settings.setValue("transpose/current-input-file", str(missing))
        settings.sync()

        restored = TransposePage(settings=settings)
        try:
            self.assertEqual(restored.input_files, [str(missing.resolve())])
            state = restored.source_states[str(missing.resolve())]
            self.assertIsNotNone(state.parse_error)
            self.assertIn("Error:", restored.file_list.item(0).toolTip())
            restored.file_list.item(0).setSelected(True)
            restored.remove_selected_files()
            self.assertEqual(settings.value("transpose/input-files", []), [])
        finally:
            restored.close()

    def test_selected_target_preset_names_both_alignment_output_modes(self):
        source = self.root / "source.kml"
        self.add_inferred_files(source)
        record = self.page.preset_repository.create(
            "Display Area",
            TranspositionPresetData().to_mapping(),
        )
        self.page._load_presets()
        state = self.page.source_states[str(source.resolve())]
        preset_id = str(record.preset.id)
        state.target_runway_preset_id = preset_id
        state.target_trace_preset_id = preset_id

        runway = RunwayTranspositionAlignment(
            RunwayReference(51.0, -1.0, 90.0, 30.0),
            RunwayReference(52.0, 0.0, 120.0),
        )
        manual = ManualTranspositionAlignment(52.0, 0.0, 35.0, 30.0)

        self.assertEqual(
            self.page._output_preset_name(str(source), runway),
            "Display Area",
        )
        self.assertEqual(
            self.page._output_preset_name(str(source), manual),
            "Display Area",
        )

    def test_run_prepares_one_per_file_runway_alignment(self):
        first = self.root / "first.kml"
        second = self.root / "second.kml"
        output_dir = self.root / "outputs"
        output_dir.mkdir()
        self.add_inferred_files(first, second)
        self.configure_target()
        outputs = tuple(
            TranspositionFileOutcome(
                input_path=path,
                planned_output_path=output_dir / f"{path.stem}.kml",
                final_output_path=output_dir / f"{path.stem}.kml",
                status=TranspositionFileStatus.SUCCEEDED,
            )
            for path in (first, second)
        )
        result = TranspositionBatchResult(outcomes=outputs)

        with (
            patch.object(
                self.page,
                "_choose_transposition_inputs",
                return_value=(str(first.resolve()), str(second.resolve())),
            ),
            patch("pages.transpose_page.parse_kml_track", return_value=inferred_track()),
            patch("pages.transpose_page.infer_departure_runway", return_value=inferred_result()),
            patch.object(QFileDialog, "getExistingDirectory", return_value=str(output_dir)),
            patch("pages.transpose_page.remember_directory") as remember_output,
            patch.object(self.page, "_edit_output_plan", side_effect=lambda plan, **_: plan),
            patch("pages.transpose_page.export_prepared_transposition", return_value=result) as run,
            patch.object(QMessageBox, "information"),
        ):
            self.page.run_transposition_ui()

        prepared_batch, plan = run.call_args.args
        self.assertEqual(len(plan.jobs), 2)
        self.assertEqual(
            tuple(job.target_airfield_slug for job in plan.jobs),
            (None, None),
        )
        self.assertTrue(
            all(
                isinstance(item, RunwayTranspositionAlignment)
                for item in self.page._prepared_alignments
            )
        )
        self.assertIsNone(prepared_batch.target_runway)
        remember_output.assert_called_once_with(
            FileDialogWorkflow.TRANSPOSITION,
            FileDialogDirection.OUTPUT,
            str(output_dir),
        )

    def test_mixed_runway_and_manual_batch_uses_per_file_alignment_and_names(self):
        runway_source = self.root / "runway-source.kml"
        manual_source = self.root / "manual-source.kml"
        output_dir = self.root / "outputs"
        output_dir.mkdir()
        self.add_inferred_files(runway_source, manual_source)
        self.configure_target()
        self.page.file_list.setCurrentRow(1)
        self.page.manual_alignment_button.click()
        self.page.target_trace_card.coordinate_input.setText("52.0, 0.25")
        self.page.target_trace_card.rotation_input.setText("35")
        self.page.original_trace_card.ground_m_input.setText("20")
        self.page._manual_form_edited()
        outputs = tuple(
            TranspositionFileOutcome(
                input_path=path,
                planned_output_path=output_dir / f"{path.stem}.kml",
                final_output_path=output_dir / f"{path.stem}.kml",
                status=TranspositionFileStatus.SUCCEEDED,
            )
            for path in (runway_source, manual_source)
        )

        with (
            patch.object(
                self.page,
                "_choose_transposition_inputs",
                return_value=(str(runway_source.resolve()), str(manual_source.resolve())),
            ),
            patch.object(QFileDialog, "getExistingDirectory", return_value=str(output_dir)),
            patch.object(self.page, "_edit_output_plan", side_effect=lambda plan, **_: plan),
            patch(
                "pages.transpose_page.export_prepared_transposition",
                return_value=TranspositionBatchResult(outcomes=outputs),
            ) as export,
            patch.object(QMessageBox, "information"),
        ):
            self.page.run_transposition_ui()

        prepared, plan = export.call_args.args
        self.assertEqual(prepared.prepared_count, 2)
        self.assertEqual(
            tuple(job.output_path.name for job in plan.jobs),
            (
                "runway-source-transposed.kml",
                "manual-source-transposed.kml",
            ),
        )
        self.assertIsInstance(
            self.page._prepared_alignments[0],
            RunwayTranspositionAlignment,
        )
        self.assertIsInstance(
            self.page._prepared_alignments[1],
            ManualTranspositionAlignment,
        )

    def test_partial_failure_dialog_reports_every_success_and_failure(self):
        first = self.root / "first.kml"
        failed = self.root / "failed.kml"
        output_dir = self.root / "outputs"
        output_dir.mkdir()
        self.add_inferred_files(first, failed)
        self.configure_target()
        success = TranspositionFileOutcome(
            input_path=first,
            planned_output_path=output_dir / "first.kml",
            final_output_path=output_dir / "first.kml",
            status=TranspositionFileStatus.SUCCEEDED,
            warnings=(
                "Omitted 2 source coordinate(s) because absolute altitude was missing.",
            ),
        )
        failure = TranspositionFileOutcome(
            input_path=failed,
            planned_output_path=output_dir / "failed.kml",
            final_output_path=None,
            status=TranspositionFileStatus.FAILED,
            error=TranspositionError(
                code=TranspositionErrorCode.INPUT_KML,
                message="invalid coordinate",
                input_path=failed,
                intended_output_path=output_dir / "failed.kml",
            ),
        )
        result = TranspositionBatchResult(outcomes=(success, failure))

        with (
            patch.object(
                self.page,
                "_choose_transposition_inputs",
                return_value=(str(first.resolve()), str(failed.resolve())),
            ),
            patch("pages.transpose_page.parse_kml_track", return_value=inferred_track()),
            patch("pages.transpose_page.infer_departure_runway", return_value=inferred_result()),
            patch.object(QFileDialog, "getExistingDirectory", return_value=str(output_dir)),
            patch.object(self.page, "_edit_output_plan", side_effect=lambda plan, **_: plan),
            patch("pages.transpose_page.export_prepared_transposition", return_value=result),
            patch.object(QMessageBox, "warning") as warning,
        ):
            self.page.run_transposition_ui()

        message = warning.call_args.args[2]
        self.assertIn("Saved 1 of 2", message)
        self.assertIn(str(success.output_path), message)
        self.assertIn(str(failed), message)
        self.assertIn("invalid coordinate", message)
        self.assertIn("first.kml:", message)
        self.assertIn("Omitted 2 source coordinate(s)", message)

    def test_success_with_processing_warnings_uses_warning_dialog(self):
        source = self.root / "source.kml"
        output_dir = self.root / "outputs"
        output_dir.mkdir()
        self.add_inferred_files(source)
        self.configure_target()
        result = TranspositionBatchResult(
            outcomes=(
                TranspositionFileOutcome(
                    input_path=source,
                    planned_output_path=output_dir / "source.kml",
                    final_output_path=output_dir / "source.kml",
                    status=TranspositionFileStatus.SUCCEEDED,
                    warnings=(
                        "Omitted 3 source coordinate(s) because absolute altitude was missing.",
                    ),
                ),
            )
        )

        with (
            patch.object(
                self.page,
                "_choose_transposition_inputs",
                return_value=(str(source.resolve()),),
            ),
            patch("pages.transpose_page.parse_kml_track", return_value=inferred_track()),
            patch("pages.transpose_page.infer_departure_runway", return_value=inferred_result()),
            patch.object(QFileDialog, "getExistingDirectory", return_value=str(output_dir)),
            patch.object(self.page, "_edit_output_plan", side_effect=lambda plan, **_: plan),
            patch("pages.transpose_page.export_prepared_transposition", return_value=result),
            patch.object(QMessageBox, "warning") as warning,
            patch.object(QMessageBox, "information") as information,
        ):
            self.page.run_transposition_ui()

        warning.assert_called_once()
        self.assertEqual(
            warning.call_args.args[1],
            "Transposition complete with warnings",
        )
        self.assertIn("source.kml:", warning.call_args.args[2])
        self.assertIn("Omitted 3 source coordinate(s)", warning.call_args.args[2])
        information.assert_not_called()

    def test_zero_success_uses_critical_dialog(self):
        source = self.root / "failed.kml"
        output_dir = self.root / "outputs"
        output_dir.mkdir()
        self.add_inferred_files(source)
        self.configure_target()
        failure = TranspositionFileOutcome(
            input_path=source,
            planned_output_path=output_dir / "failed.kml",
            final_output_path=None,
            status=TranspositionFileStatus.FAILED,
            error=TranspositionError(
                code=TranspositionErrorCode.INPUT_KML,
                message="invalid coordinate",
                input_path=source,
                intended_output_path=output_dir / "failed.kml",
            ),
        )
        result = TranspositionBatchResult(outcomes=(failure,))

        with (
            patch.object(
                self.page,
                "_choose_transposition_inputs",
                return_value=(str(source.resolve()),),
            ),
            patch.object(QFileDialog, "getExistingDirectory", return_value=str(output_dir)),
            patch.object(self.page, "_edit_output_plan", side_effect=lambda plan, **_: plan),
            patch("pages.transpose_page.export_prepared_transposition", return_value=result),
            patch.object(QMessageBox, "critical") as critical,
            patch.object(QMessageBox, "information") as information,
        ):
            self.page.run_transposition_ui()

        critical.assert_called_once()
        self.assertIn("No KML files were produced", critical.call_args.args[2])
        information.assert_not_called()

    def test_malformed_input_remains_a_per_file_failure_candidate(self):
        source = self.root / "broken.kml"
        with patch(
            "pages.transpose_page.parse_kml_track",
            side_effect=KmlStructureError("No path geometry"),
        ):
            self.page.add_files_to_list([str(source)])

        self.assertEqual(self.page.source_card.status_label.text(), "File error")
        self.assertEqual(self.page._review_source_runways(), (None,))

    def test_output_dialog_still_validates_every_filename(self):
        output_dir = self.root / "outputs"
        output_dir.mkdir()
        plan = create_transposition_plan(
            [self.root / "first.kml", self.root / "second.kml"],
            output_dir,
            "Fairford",
        )
        dialog = TranspositionOutputDialog(plan, self.page)
        dialog.filename_edits[0].setText("Same.kml")
        dialog.filename_edits[1].setText("same.KML")
        dialog._validate_and_accept()
        self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertIn("unique", dialog.error_label.text())
        dialog.close()

    def test_output_dialog_fits_editors_using_live_font_and_screen_metrics(self):
        output_dir = self.root / "outputs"
        output_dir.mkdir()
        plan = create_transposition_plan(
            [self.root / f"input-{index}.kml" for index in range(8)],
            output_dir,
            "Fairford",
        )
        dialog = TranspositionOutputDialog(plan, self.page)
        larger_font = dialog.font()
        larger_font.setPointSize(larger_font.pointSize() + 6)
        dialog.setFont(larger_font)
        dialog.show()
        self.app.processEvents()

        for row, edit in enumerate(dialog.filename_edits):
            self.assertGreaterEqual(dialog.table.rowHeight(row), edit.sizeHint().height())
            self.assertGreaterEqual(edit.height(), edit.sizeHint().height())
        self.assertGreaterEqual(
            dialog.table.height(),
            dialog.table.horizontalHeader().height() + dialog.table.rowHeight(0),
        )
        available = dialog.screen().availableGeometry()
        self.assertLessEqual(dialog.width(), available.width())
        self.assertLessEqual(dialog.height(), available.height())
        dialog.close()

    def test_existing_custom_output_requires_explicit_overwrite_confirmation(self):
        output_dir = self.root / "outputs"
        output_dir.mkdir()
        plan = create_transposition_plan([self.root / "first.kml"], output_dir, "Fairford")
        destination = output_dir / "chosen.kml"
        destination.write_text("existing", encoding="utf-8")
        from services import customize_transposition_plan

        candidate = customize_transposition_plan(plan, (destination.name,))
        with (
            patch("pages.transpose_page.TranspositionOutputDialog") as dialog_class,
            patch.object(
                QMessageBox,
                "question",
                return_value=QMessageBox.StandardButton.Yes,
            ),
        ):
            dialog_class.return_value.exec.return_value = QDialog.DialogCode.Accepted
            dialog_class.return_value.validated_plan = candidate
            approved = self.page._edit_output_plan(plan)

        self.assertTrue(approved.jobs[0].overwrite_existing)
        self.assertEqual(approved.jobs[0].output_path, destination)


class AirfieldPresetManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        from services import PresetImportExportService, PresetRepository

        self.repository = PresetRepository(self.root / "presets", PresetType.AIRFIELD)
        self.dialog = AirfieldPresetManagerDialog(
            self.repository,
            PresetImportExportService(self.repository),
        )

    def tearDown(self):
        self.dialog.close()
        self.tempdir.cleanup()

    def fill_editor(self):
        form = self.dialog.editor_form
        form.coordinate_input.setText("51.0, -1.0")
        form.heading_input.setText("90")
        form.elevation_m_input.setText("30")

    def test_manager_saves_named_structured_runway_section(self):
        self.fill_editor()
        with patch.object(
            QInputDialog, "getText", return_value=("Display Field", True)
        ):
            self.dialog._save_editor()
        record = next(iter(self.repository.load_all().values()))
        self.assertEqual(record.preset.name, "Display Field")
        self.assertEqual(record.preset.data["data_version"], 2)
        self.assertEqual(record.preset.data["runway"]["elevation_m"], 30.0)


if __name__ == "__main__":
    unittest.main()
