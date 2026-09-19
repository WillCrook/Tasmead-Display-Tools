import os
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from threading import Event
from unittest.mock import patch


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from services.kml_editor_workspace import (
    EditorMode,
    KmlEditorWorkspaceModel,
    OperationStatus,
    ParseStatus,
)
from services.kml_editor_operations import (
    KmlEditorOperationCancelled,
    simplify_track as simplify_track_now,
)
from PyQt6.QtTest import QTest
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication


VALID_KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark>
<LineString><coordinates>-1,51,10 -1.1,51.1,20 -1.2,51.2,30</coordinates></LineString>
</Placemark></Document></kml>
"""


class KmlEditorWorkspaceModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.first = self.root / "first.kml"
        self.second = self.root / "second.kml"
        self.first.write_text(VALID_KML, encoding="utf-8", newline="\n")
        self.second.write_text(
            VALID_KML.replace("-1.2,51.2,30", "-2,52,40"),
            encoding="utf-8",
            newline="\n",
        )
        self.model = KmlEditorWorkspaceModel(validation_debounce_ms=20)

    def tearDown(self):
        self.model._validation_pool.waitForDone(1000)
        self.model._operation_executor.wait_for_done(1.0)
        self.app.processEvents()
        self.model.shutdown()
        self.temp_dir.cleanup()

    def wait_for_validation(self, *document_ids, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if all(
                self.model.document(document_id).parse_state.status
                not in {ParseStatus.STALE, ParseStatus.VALIDATING}
                for document_id in document_ids
            ):
                return
            QTest.qWait(5)
        self.fail("validation did not finish")

    def wait_for_operations(self, document_id, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            document = self.model.document(document_id)
            busy = {
                document.crop_state.status,
                document.simplification_state.status,
            } & {OperationStatus.PREPARING, OperationStatus.APPLYING}
            if not busy and not any(key[0] == document_id for key in self.model._operation_tasks):
                return
            QTest.qWait(5)
        self.fail("editor operation did not finish")

    def test_open_is_read_only_parses_and_duplicate_focuses_existing_document(self):
        original = self.first.read_bytes()
        original_mtime = self.first.stat().st_mtime_ns

        result = self.model.add_paths([self.first])
        duplicate = self.model.add_paths([self.first])
        self.wait_for_validation(*result.document_ids)

        self.assertEqual(len(self.model.documents), 1)
        self.assertEqual(result.document_ids, duplicate.document_ids)
        self.assertEqual(self.first.read_bytes(), original)
        self.assertEqual(self.first.stat().st_mtime_ns, original_mtime)
        document = self.model.active_document
        self.assertIsNotNone(document)
        self.assertEqual(document.parse_state.status, ParseStatus.VALID)
        self.assertEqual(document.parse_state.point_count, 3)
        self.assertEqual((document.crop_state.start_index, document.crop_state.end_index), (0, 2))

    def test_malformed_kml_remains_loaded_with_diagnostic_state(self):
        malformed = self.root / "malformed.kml"
        malformed.write_text("<kml><broken>", encoding="utf-8")

        result = self.model.add_paths([malformed])
        self.wait_for_validation(*result.document_ids)

        self.assertFalse(result.errors)
        self.assertEqual(self.model.active_document.parse_state.status, ParseStatus.INVALID)
        self.assertTrue(self.model.active_document.parse_state.diagnostics)

    def test_active_switching_and_all_per_file_state_are_isolated(self):
        ids = self.model.add_paths([self.first, self.second]).document_ids
        first_id, second_id = ids
        self.wait_for_validation(*ids)
        first_text = self.model.document(first_id).contents

        self.model.update_contents(first_id, first_text + "<!-- edit -->")
        self.model.update_simplification_tolerance(first_id, 25.0)
        self.model.set_active_document(second_id)
        self.model.update_crop(second_id, 1, 2)
        self.model.set_mode(EditorMode.CROP)

        first_state = self.model.document(first_id)
        second_state = self.model.document(second_id)
        self.assertTrue(first_state.dirty)
        self.assertEqual(first_state.parse_state.status, ParseStatus.STALE)
        self.assertEqual(first_state.simplification_state.tolerance_m, 25.0)
        self.assertFalse(second_state.dirty)
        self.assertEqual(second_state.crop_state.start_index, 1)
        self.assertEqual(second_state.simplification_state.tolerance_m, 5.0)
        self.assertEqual(self.model.active_document_id, second_id)
        self.assertEqual(self.model.mode, EditorMode.CROP)

    def test_returning_text_to_snapshot_restores_saved_parse_state(self):
        document_id = self.model.add_paths([self.first]).document_ids[0]
        self.wait_for_validation(document_id)
        saved = self.model.document(document_id).saved_contents
        self.model.update_contents(document_id, saved + "<!-- edit -->")
        self.assertEqual(self.model.document(document_id).parse_state.status, ParseStatus.STALE)

        self.model.update_contents(document_id, saved)

        document = self.model.document(document_id)
        self.assertFalse(document.dirty)
        self.assertEqual(document.parse_state.status, ParseStatus.VALID)

    def test_restore_uses_snapshot_without_rereading_or_writing_disk(self):
        document_id = self.model.add_paths([self.first]).document_ids[0]
        self.wait_for_validation(document_id)
        saved = self.model.document(document_id).saved_contents
        self.model.update_contents(document_id, saved + "<!-- edit -->")
        external = VALID_KML.replace("-1,51,10", "-3,53,10")
        self.first.write_text(external, encoding="utf-8")
        disk_before_restore = self.first.read_bytes()

        self.model.restore_document(document_id)

        document = self.model.document(document_id)
        self.assertEqual(document.contents, saved)
        self.assertFalse(document.dirty)
        self.assertEqual(self.first.read_bytes(), disk_before_restore)

    def test_save_is_atomic_clears_dirty_and_reparses_written_contents(self):
        document_id = self.model.add_paths([self.first]).document_ids[0]
        invalid = "<kml><broken>"
        self.model.update_contents(document_id, invalid)

        self.model.save_document(document_id)
        self.wait_for_validation(document_id)

        document = self.model.document(document_id)
        self.assertEqual(self.first.read_text(encoding="utf-8"), invalid)
        self.assertFalse(document.dirty)
        self.assertEqual(document.parse_state.status, ParseStatus.INVALID)
        self.assertEqual(list(self.root.glob(".first.kml.*.tmp")), [])

    def test_save_as_keeps_identity_updates_path_and_rejects_an_open_destination(self):
        first_id, second_id = self.model.add_paths([self.first, self.second]).document_ids
        self.wait_for_validation(first_id, second_id)
        original_id = first_id
        destination = self.root / "copy.kml"

        self.model.save_document(first_id, destination)

        self.assertEqual(self.model.document(first_id).document_id, original_id)
        self.assertEqual(self.model.document(first_id).source_path, destination.resolve())
        self.assertTrue(destination.exists())
        with self.assertRaises(FileExistsError):
            self.model.save_document(first_id, self.model.document(second_id).source_path)

    def test_remove_updates_active_document_without_leaking_state(self):
        first_id, second_id = self.model.add_paths([self.first, self.second]).document_ids
        self.model.set_active_document(first_id)

        self.model.remove_documents([first_id])

        self.assertEqual(tuple(item.document_id for item in self.model.documents), (second_id,))
        self.assertEqual(self.model.active_document_id, second_id)

    def test_validation_is_debounced_and_superseded_results_are_rejected(self):
        first_started = Event()
        release_first = Event()

        def validator(contents, *, source_name):
            if "slow-invalid" in contents:
                first_started.set()
                release_first.wait(1.0)
            from services import parse_kml_text
            return parse_kml_text(contents, source_name=source_name)

        self.model._validation_pool.waitForDone(1000)
        self.model = KmlEditorWorkspaceModel(
            validator=validator,
            validation_debounce_ms=10,
        )
        document_id = self.model.add_paths([self.first]).document_ids[0]
        self.wait_for_validation(document_id)
        valid = self.model.document(document_id).contents

        self.model.update_contents(document_id, valid + "slow-invalid")
        self.assertEqual(self.model.document(document_id).parse_state.status, ParseStatus.STALE)
        deadline = time.monotonic() + 1.0
        while not first_started.is_set() and time.monotonic() < deadline:
            QTest.qWait(5)
        self.assertTrue(first_started.is_set())
        main_events = []
        QTimer.singleShot(0, lambda: main_events.append("processed"))
        QTest.qWait(10)
        self.assertEqual(main_events, ["processed"])
        second_id = self.model.add_paths([self.second]).document_ids[0]
        self.model.set_active_document(second_id)
        self.model.update_contents(document_id, valid.replace("Track", "Track"))
        self.model.validate_document_now(document_id)
        release_first.set()
        self.wait_for_validation(document_id, second_id)

        document = self.model.document(document_id)
        self.assertEqual(document.parse_state.status, ParseStatus.VALID)
        self.assertEqual(document.parse_state.revision, document.revision)
        self.assertEqual(self.model.active_document_id, second_id)
        self.assertEqual(self.model.active_document.source_path, self.second.resolve())

    def test_cursor_state_is_kept_per_document(self):
        first_id, second_id = self.model.add_paths([self.first, self.second]).document_ids
        self.model.update_cursor(first_id, 12, 5)
        self.model.update_cursor(second_id, 30)
        self.assertEqual(
            (self.model.document(first_id).cursor_position, self.model.document(first_id).cursor_anchor),
            (12, 5),
        )
        self.assertEqual(self.model.document(second_id).cursor_position, 30)

    def test_failed_atomic_replace_preserves_disk_and_dirty_snapshot(self):
        document_id = self.model.add_paths([self.first]).document_ids[0]
        self.wait_for_validation(document_id)
        original = self.first.read_bytes()
        self.model.update_contents(document_id, VALID_KML.replace("-1,51,10", "-3,53,10"))

        with patch("services.kml_editor_workspace.os.replace", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.model.save_document(document_id)

        document = self.model.document(document_id)
        self.assertTrue(document.dirty)
        self.assertEqual(self.first.read_bytes(), original)
        self.assertEqual(list(self.root.glob(".first.kml.*.tmp")), [])

    def test_crop_apply_changes_only_memory_until_save_and_restore_is_reversible(self):
        document_id = self.model.add_paths([self.first]).document_ids[0]
        self.wait_for_validation(document_id)
        original_disk = self.first.read_bytes()
        self.model.update_crop(document_id, 1, 2)

        self.assertTrue(self.model.apply_crop(document_id))
        self.wait_for_operations(document_id)
        self.wait_for_validation(document_id)

        document = self.model.document(document_id)
        self.assertTrue(document.dirty)
        self.assertEqual(document.parse_state.point_count, 2)
        self.assertEqual(
            (document.crop_state.start_index, document.crop_state.end_index),
            (0, 1),
        )
        self.assertTrue(self.model.request_crop_preview(document_id))
        self.wait_for_operations(document_id)
        self.assertEqual(
            self.model.document(document_id).crop_state.status,
            OperationStatus.READY,
        )
        self.assertEqual(self.first.read_bytes(), original_disk)
        self.model.restore_document(document_id)
        self.assertEqual(self.model.document(document_id).contents, VALID_KML)
        self.assertEqual(self.first.read_bytes(), original_disk)

    def test_validation_preserves_valid_crop_and_resets_a_collapsed_range(self):
        document_id = self.model.add_paths([self.first]).document_ids[0]
        self.wait_for_validation(document_id)

        self.model.update_crop(document_id, 0, 1)
        changed = self.model.document(document_id).contents.replace("-1,51,10", "-1,51,11")
        self.model.update_contents(document_id, changed)
        self.wait_for_validation(document_id)
        document = self.model.document(document_id)
        self.assertEqual(
            (document.crop_state.start_index, document.crop_state.end_index),
            (0, 1),
        )

        self.model.update_crop(document_id, 1, 2)
        shortened = document.contents.replace("-1,51,11 ", "")
        self.model.update_contents(document_id, shortened)
        self.wait_for_validation(document_id)
        document = self.model.document(document_id)
        self.assertEqual(document.parse_state.point_count, 2)
        self.assertEqual(
            (document.crop_state.start_index, document.crop_state.end_index),
            (0, 1),
        )

    def test_invalid_crop_range_is_a_recoverable_preview_and_apply_error(self):
        document_id = self.model.add_paths([self.first]).document_ids[0]
        self.wait_for_validation(document_id)
        document = self.model.document(document_id)
        invalid_crop = replace(document.crop_state, start_index=2, end_index=2)
        self.model._documents[document_id] = replace(document, crop_state=invalid_crop)

        self.assertFalse(self.model.request_crop_preview(document_id))
        preview_error = self.model.document(document_id).crop_state
        self.assertEqual(preview_error.status, OperationStatus.ERROR)
        self.assertIn("at least two", preview_error.error)
        self.assertEqual(
            preview_error.operation_revision,
            invalid_crop.operation_revision + 1,
        )

        self.assertFalse(self.model.apply_crop(document_id))
        apply_error = self.model.document(document_id).crop_state
        self.assertEqual(apply_error.status, OperationStatus.ERROR)
        self.assertIn("at least two", apply_error.error)
        self.assertEqual(
            apply_error.operation_revision,
            preview_error.operation_revision + 1,
        )

    def test_simplification_result_is_revision_bound_and_calculated_off_thread(self):
        document_id = self.model.add_paths([self.first]).document_ids[0]
        self.wait_for_validation(document_id)
        heartbeat = []
        QTimer.singleShot(0, lambda: heartbeat.append(True))

        self.assertTrue(self.model.request_simplification(document_id))
        QTest.qWait(10)
        self.assertEqual(heartbeat, [True])
        self.wait_for_operations(document_id)

        state = self.model.document(document_id).simplification_state
        self.assertEqual(state.status, OperationStatus.READY)
        self.assertEqual(state.result_revision, self.model.document(document_id).revision)
        self.assertEqual(state.kept_indices[0], 0)
        self.assertEqual(state.kept_indices[-1], 2)
        old_revision = state.result_revision
        self.model.update_contents(document_id, self.model.document(document_id).contents + "<!-- edit -->")
        state = self.model.document(document_id).simplification_state
        self.assertIsNone(state.result_revision)
        self.assertNotEqual(self.model.document(document_id).revision, old_revision)

    def test_simplification_churn_cancels_every_superseded_task(self):
        document_id = self.model.add_paths([self.first]).document_ids[0]
        self.wait_for_validation(document_id)
        started = Event()
        release = Event()
        cancelled = Event()
        tolerances = []

        def controlled_simplification(track, tolerance_m, *, cancellation_check=None):
            tolerances.append(tolerance_m)
            if len(tolerances) == 1:
                started.set()
                release.wait(1.0)
                if cancellation_check is not None and cancellation_check():
                    cancelled.set()
                    raise KmlEditorOperationCancelled()
            return simplify_track_now(
                track,
                tolerance_m,
                cancellation_check=cancellation_check,
            )

        with patch(
            "services.kml_editor_workspace.simplify_track",
            side_effect=controlled_simplification,
        ):
            self.assertTrue(self.model.request_simplification(document_id))
            deadline = time.monotonic() + 1.0
            while not started.is_set() and time.monotonic() < deadline:
                QTest.qWait(5)
            self.assertTrue(started.is_set())

            self.model.update_simplification_tolerance(document_id, 10.0)
            self.assertTrue(self.model.request_simplification(document_id))
            self.model.update_simplification_tolerance(document_id, 20.0)
            self.assertTrue(self.model.request_simplification(document_id))
            release.set()
            self.wait_for_operations(document_id)

        state = self.model.document(document_id).simplification_state
        self.assertTrue(cancelled.is_set())
        self.assertEqual(tolerances, [5.0, 20.0])
        self.assertEqual(state.tolerance_m, 20.0)
        self.assertEqual(state.status, OperationStatus.READY)
        self.assertEqual(state.result_revision, self.model.document(document_id).revision)
        self.assertFalse(self.model._operation_tasks)

    def test_shutdown_is_non_blocking_and_discards_late_operation_results(self):
        document_id = self.model.add_paths([self.first]).document_ids[0]
        self.wait_for_validation(document_id)
        started = Event()
        release = Event()

        def blocked_simplification(track, tolerance_m, *, cancellation_check=None):
            started.set()
            release.wait(1.0)
            if cancellation_check is not None and cancellation_check():
                raise KmlEditorOperationCancelled()
            return simplify_track_now(track, tolerance_m)

        try:
            with patch(
                "services.kml_editor_workspace.simplify_track",
                side_effect=blocked_simplification,
            ):
                self.assertTrue(self.model.request_simplification(document_id))
                deadline = time.monotonic() + 1.0
                while not started.is_set() and time.monotonic() < deadline:
                    QTest.qWait(5)
                self.assertTrue(started.is_set())
                before_shutdown = self.model.document(document_id)

                shutdown_started = time.monotonic()
                self.model.shutdown()
                shutdown_seconds = time.monotonic() - shutdown_started

                self.assertLess(shutdown_seconds, 0.25)
                self.assertFalse(self.model.request_simplification(document_id))
                release.set()
                self.assertTrue(self.model._operation_executor.wait_for_done(1.0))
                self.app.processEvents()

            self.assertEqual(self.model.document(document_id), before_shutdown)
            self.assertFalse(self.model._operation_tasks)
        finally:
            release.set()

    def test_cancel_invalidates_a_completed_but_not_yet_delivered_apply(self):
        document_id = self.model.add_paths([self.first]).document_ids[0]
        self.wait_for_validation(document_id)
        original = self.model.document(document_id).contents

        self.assertTrue(
            self.model.request_simplification(document_id, purpose="apply")
        )
        self.assertTrue(self.model._operation_executor.wait_for_done(1.0))
        self.model.cancel_operations(document_id)
        self.app.processEvents()

        document = self.model.document(document_id)
        self.assertEqual(document.contents, original)
        self.assertFalse(document.dirty)
        self.assertEqual(
            document.simplification_state.status,
            OperationStatus.CANCELLED,
        )

    def test_file_switch_discards_a_completed_but_undelivered_preview(self):
        first_id, second_id = self.model.add_paths([self.first, self.second]).document_ids
        self.wait_for_validation(first_id, second_id)
        previews = []
        self.model.preview_ready.connect(lambda *args: previews.append(args))
        self.model.update_crop(first_id, 0, 1)

        self.assertTrue(self.model.request_crop_preview(first_id))
        self.assertTrue(self.model._operation_executor.wait_for_done(1.0))
        self.model.set_active_document(second_id)
        self.app.processEvents()

        self.assertEqual(previews, [])
        self.assertEqual(
            self.model.document(first_id).crop_state.status,
            OperationStatus.CANCELLED,
        )

    def test_result_for_a_removed_document_is_ignored(self):
        started = Event()
        release = Event()

        def validator(contents, *, source_name):
            started.set()
            release.wait(1.0)
            from services import parse_kml_text
            return parse_kml_text(contents, source_name=source_name)

        self.model._validation_pool.waitForDone(1000)
        self.model = KmlEditorWorkspaceModel(validator=validator)
        document_id = self.model.add_paths([self.first]).document_ids[0]
        deadline = time.monotonic() + 1.0
        while not started.is_set() and time.monotonic() < deadline:
            QTest.qWait(5)
        self.assertTrue(started.is_set())

        self.model.remove_documents([document_id])
        release.set()
        self.model._validation_pool.waitForDone(1000)
        self.app.processEvents()

        self.assertEqual(self.model.documents, ())
        self.assertIsNone(self.model.active_document_id)

    def test_save_preserves_declared_encoding_and_crlf_newlines(self):
        path = self.root / "latin.kml"
        source = VALID_KML.replace(
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<?xml version="1.0" encoding="ISO-8859-1"?>',
        ).replace("<Placemark>", "<Placemark><name>Café</name>")
        path.write_bytes(source.replace("\n", "\r\n").encode("iso-8859-1"))
        document_id = self.model.add_paths([path]).document_ids[0]
        self.wait_for_validation(document_id)

        self.model.update_contents(
            document_id,
            self.model.document(document_id).contents.replace("Café", "Cafè"),
        )
        self.model.save_document(document_id)

        saved = path.read_bytes()
        self.assertIn(b"Caf\xe8", saved)
        self.assertIn(b"\r\n", saved)
        self.assertNotIn(b"\n", saved.replace(b"\r\n", b""))


if __name__ == "__main__":
    unittest.main()
