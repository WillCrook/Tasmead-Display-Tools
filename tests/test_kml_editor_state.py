import os
import sys
import tempfile
import unittest
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from services.kml_editor_workspace import (
    EditorMode,
    KmlEditorWorkspaceModel,
    ParseStatus,
)


VALID_KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark>
<LineString><coordinates>-1,51,10 -1.1,51.1,20 -1.2,51.2,30</coordinates></LineString>
</Placemark></Document></kml>
"""


class KmlEditorWorkspaceModelTests(unittest.TestCase):
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
        self.model = KmlEditorWorkspaceModel()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_open_is_read_only_parses_and_duplicate_focuses_existing_document(self):
        original = self.first.read_bytes()
        original_mtime = self.first.stat().st_mtime_ns

        result = self.model.add_paths([self.first])
        duplicate = self.model.add_paths([self.first])

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

        self.assertFalse(result.errors)
        self.assertEqual(self.model.active_document.parse_state.status, ParseStatus.INVALID)
        self.assertTrue(self.model.active_document.parse_state.diagnostics)

    def test_active_switching_and_all_per_file_state_are_isolated(self):
        ids = self.model.add_paths([self.first, self.second]).document_ids
        first_id, second_id = ids
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
        self.assertEqual(second_state.simplification_state.tolerance_m, 10.0)
        self.assertEqual(self.model.active_document_id, second_id)
        self.assertEqual(self.model.mode, EditorMode.CROP)

    def test_returning_text_to_snapshot_restores_saved_parse_state(self):
        document_id = self.model.add_paths([self.first]).document_ids[0]
        saved = self.model.document(document_id).saved_contents
        self.model.update_contents(document_id, saved + "<!-- edit -->")
        self.assertEqual(self.model.document(document_id).parse_state.status, ParseStatus.STALE)

        self.model.update_contents(document_id, saved)

        document = self.model.document(document_id)
        self.assertFalse(document.dirty)
        self.assertEqual(document.parse_state.status, ParseStatus.VALID)

    def test_restore_uses_snapshot_without_rereading_or_writing_disk(self):
        document_id = self.model.add_paths([self.first]).document_ids[0]
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

        document = self.model.document(document_id)
        self.assertEqual(self.first.read_text(encoding="utf-8"), invalid)
        self.assertFalse(document.dirty)
        self.assertEqual(document.parse_state.status, ParseStatus.INVALID)
        self.assertEqual(list(self.root.glob(".first.kml.*.tmp")), [])

    def test_save_as_keeps_identity_updates_path_and_rejects_an_open_destination(self):
        first_id, second_id = self.model.add_paths([self.first, self.second]).document_ids
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


if __name__ == "__main__":
    unittest.main()
