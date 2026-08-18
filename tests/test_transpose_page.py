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
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QInputDialog,
    QMessageBox,
    QPushButton,
)

from file_dialog_state import FileDialogDirection, FileDialogWorkflow
from pages.airfield_ui import AirfieldPresetManagerDialog
from pages.transpose_page import (
    TransposePage,
    TranspositionInputDialog,
    TranspositionOutputDialog,
)
from services import (
    KmlPoint,
    KmlStructureError,
    KmlTrack,
    PresetType,
    RunwayCandidate,
    RunwayConfidence,
    RunwayInferenceResult,
    RunwayReference,
    TranspositionBatchResult,
    TranspositionError,
    TranspositionErrorCode,
    TranspositionFileOutcome,
    TranspositionFileStatus,
    TraceAdjustment,
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
        self.page = TransposePage()

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

    def configure_target(self, runway="24"):
        self.page.target_card.name_input.setText("RAF Fairford")
        self.page.target_card.runway_input.setText(runway)
        self.page.target_card.coordinate_input.setText("51.0, -1.0")
        self.page.target_card.heading_input.setText("240")

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

    def test_target_airfield_hosts_transposition_and_connected_preview_buttons(self):
        self.page.show()
        self.app.processEvents()

        self.assertTrue(self.page.target_card.isAncestorOf(self.page.run_btn))
        self.assertTrue(self.page.target_card.isAncestorOf(self.page.preview_btn))
        self.assertLess(self.page.preview_btn.x(), self.page.run_btn.x())
        self.assertEqual(self.page.run_btn.text(), "Transpose files")
        self.assertEqual(self.page.run_btn.objectName(), "primaryButton")
        self.assertEqual(self.page.preview_btn.text(), "View preview")
        self.assertTrue(self.page.preview_btn.isEnabled())
        self.assertEqual(
            self.page.preview_btn.receivers(self.page.preview_btn.clicked),
            1,
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

    def test_preview_prepares_only_the_current_file(self):
        first = self.root / "first.kml"
        unrelated = self.root / "broken.kml"
        self.add_inferred_files(first, unrelated)
        unrelated_state = self.page.source_states[str(unrelated.resolve())]
        unrelated_state.analysed = True
        unrelated_state.parse_error = "Unrelated file is broken."
        self.page._sync_file_item_error(unrelated)
        self.configure_target()
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
            (str(first.resolve()),),
        )
        self.assertEqual(len(scenes), 1)
        self.assertEqual(len(scenes[0].traces), 1)
        warning.assert_not_called()

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
        self.page.source_card._show_details()
        self.assertTrue(self.page.source_card._details_popup.isVisible())
        self.page.source_card._details_popup.close()
        self.assertIn("Heading High", self.page.source_card.confidence_label.text())

        self.page.source_card.name_input.setText("Manual source")
        self.page._source_form_edited()
        first_state = self.page.source_states[str(first.resolve())]
        self.assertEqual(first_state.provenance, "Manual override")

        with (
            patch("pages.transpose_page.parse_kml_track", return_value=inferred_track()),
            patch("pages.transpose_page.infer_departure_runway", return_value=inferred_result()),
        ):
            self.page.file_list.setCurrentRow(1)
        self.page.file_list.setCurrentRow(0)
        self.assertEqual(self.page.source_card.name_input.text(), "Manual source")
        self.assertFalse(self.page.source_card.restore_button.isHidden())

        self.page._restore_auto_source()
        self.assertEqual(self.page.source_card.name_input.text(), "")
        self.assertEqual(self.page.source_card.status_label.text(), "Auto-detected")

    def test_materially_replaced_source_drops_its_committed_adjustment(self):
        source = self.root / "source.kml"
        self.add_inferred_files(source)
        source_runway = RunwayReference(51.0, -1.0, 90.0, 30.0)
        target_runway = RunwayReference(51.1, -0.8, 120.0)
        batch = prepare_transposition(
            input_files=(source,),
            source_runways=(source_runway,),
            target_runway=target_runway,
        )
        key = self.page._path_key(source)
        adjustment = TraceAdjustment(east_m=20.0)
        self.page._committed_adjustments[key] = (
            self.page._source_fingerprint(source),
            adjustment,
        )
        adjusted = self.page._apply_committed_adjustments(batch)
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
        reapplied = self.page._apply_committed_adjustments(replacement)

        self.assertTrue(reapplied.prepared[0].trace.adjustment.is_zero)
        self.assertNotIn(key, self.page._committed_adjustments)

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
        self.assertEqual(self.page.target_card.name_input.text(), "Farnborough")
        self.assertFalse(self.page.target_card.include_elevation)
        self.assertFalse(
            any(
                button.text() == "Apply"
                for card in (self.page.source_card, self.page.target_card)
                for button in card.findChildren(QPushButton)
            )
        )

    def test_nonstandard_runways_are_confirmed_once_and_cancel_stops_run(self):
        source = self.root / "source.kml"
        self.add_inferred_files(source)
        self.page.source_card.runway_input.setText("DISPLAY-A")
        self.page._source_form_edited()
        self.configure_target(runway="TEMP")

        with (
            patch.object(
                self.page,
                "_choose_transposition_inputs",
                return_value=(str(source.resolve()),),
            ),
            patch.object(
                QMessageBox,
                "question",
                return_value=QMessageBox.StandardButton.Cancel,
            ) as question,
            patch.object(QFileDialog, "getExistingDirectory") as folder,
        ):
            self.page.run_transposition_ui()

        question.assert_called_once()
        message = question.call_args.args[2]
        self.assertIn("DISPLAY-A", message)
        self.assertIn("TEMP", message)
        folder.assert_not_called()

    def test_conventional_suffix_is_normalised_without_override_prompt(self):
        source = self.root / "source.kml"
        self.add_inferred_files(source)
        self.configure_target(runway="24r")

        with (
            patch.object(
                self.page,
                "_choose_transposition_inputs",
                return_value=(str(source.resolve()),),
            ),
            patch.object(QMessageBox, "question") as question,
            patch.object(QFileDialog, "getExistingDirectory", return_value=""),
        ):
            self.page.run_transposition_ui()

        self.assertEqual(self.page.target_card.runway_input.text(), "24R")
        question.assert_not_called()

    def test_run_attaches_one_inline_reviewed_source_per_job(self):
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
        self.assertTrue(all(job.source_runway is not None for job in plan.jobs))
        self.assertEqual(prepared_batch.target_runway.elevation_m, None)
        remember_output.assert_called_once_with(
            FileDialogWorkflow.TRANSPOSITION,
            FileDialogDirection.OUTPUT,
            str(output_dir),
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

    def fill_editor(self, runway):
        form = self.dialog.editor_form
        form.name_input.setText("Display Field")
        form.runway_input.setText(runway)
        form.coordinate_input.setText("51.0, -1.0")
        form.heading_input.setText("90")
        form.elevation_m_input.setText("30")

    def test_nonstandard_preset_save_requires_confirmation_each_time(self):
        self.fill_editor("DISPLAY-A")
        with patch.object(
            QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.Cancel,
        ) as question:
            self.dialog._save_editor()
        question.assert_called_once()
        self.assertEqual(self.repository.load_all(), {})

        with (
            patch.object(
                QMessageBox,
                "question",
                return_value=QMessageBox.StandardButton.Yes,
            ) as question,
            patch.object(QInputDialog, "getText", return_value=("Display Field", True)),
        ):
            self.dialog._save_editor()
        question.assert_called_once()
        record = next(iter(self.repository.load_all().values()))
        self.assertEqual(record.preset.data["runway"], "DISPLAY-A")


if __name__ == "__main__":
    unittest.main()
