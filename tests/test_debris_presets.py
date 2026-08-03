import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QFileDialog, QMessageBox
from pages.debris_page import DebrisPage
from pages.transpose_page import TransposePage
from services.preset_store import PresetStore
import resource_paths


class DebrisPresetOwnershipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.user_data_root = Path(self.temp_dir.name) / "user-data"
        self.legacy_root = Path(self.temp_dir.name) / "legacy-resources"
        self.resource_path = lambda relative: str(self.legacy_root / relative)
        self.app_data_path = lambda relative: str(self.user_data_root / relative)
        self.resource_patch = patch("pages.debris_page.resource_path", self.resource_path)
        self.app_data_patch = patch("pages.debris_page.app_data_path", self.app_data_path)
        self.resource_patch.start()
        self.app_data_patch.start()
        self.page = DebrisPage()

    def tearDown(self):
        self.page.close()
        self.app_data_patch.stop()
        self.resource_patch.stop()
        self.temp_dir.cleanup()

    @staticmethod
    def preset_data(mass="10"):
        return {"config": {"Mass (kg)": mass}, "flight_mode": "kml"}

    def import_file(self, path):
        with patch.object(QFileDialog, "getOpenFileName", return_value=(str(path), "JSON Files (*.json)")):
            self.page.load_preset_from_file()

    def select_entry(self, preset_id):
        for row in range(self.page.preset_list.count()):
            item = self.page.preset_list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == preset_id:
                self.page.preset_list.setCurrentItem(item)
                self.page.load_selected_preset(item)
                return
        self.fail(f"Preset {preset_id} was not listed")

    def test_imported_preset_removal_never_deletes_source(self):
        source = Path(self.temp_dir.name) / "external.json"
        source.write_text(json.dumps(self.preset_data()), encoding="utf-8")
        original = source.read_text(encoding="utf-8")

        self.import_file(source)
        preset_id = next(iter(self.page.presets))
        self.select_entry(preset_id)
        self.assertEqual(self.page.inputs["Mass (kg)"].text(), "10")
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.page.delete_preset()

        self.assertTrue(source.exists())
        self.assertEqual(source.read_text(encoding="utf-8"), original)
        self.assertNotIn(preset_id, self.page.presets)

    def test_app_managed_preset_deletion_still_deletes_its_file(self):
        self.assertEqual(Path(self.page.presets_dir), self.user_data_root / "debris-presets")
        self.assertNotEqual(Path(self.page.presets_dir), self.legacy_root / "data" / "presets")
        entry = self.page.preset_store.save("saved", self.preset_data())
        self.page.load_presets_from_disk()
        preset_id = self.page.managed_preset_id("saved")
        self.select_entry(preset_id)

        self.page.delete_preset()

        self.assertFalse(Path(entry["path"]).exists())
        self.assertNotIn(preset_id, self.page.presets)

    def test_collision_replaces_only_app_managed_copy(self):
        managed = self.page.preset_store.save("shared", self.preset_data("1"))
        source = Path(self.temp_dir.name) / "shared.json"
        source.write_text(json.dumps(self.preset_data("2")), encoding="utf-8")
        original = source.read_text(encoding="utf-8")
        self.page.load_presets_from_disk()

        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.import_file(source)

        self.assertEqual(json.loads(Path(managed["path"]).read_text())["config"]["Mass (kg)"], "2")
        self.assertEqual(source.read_text(encoding="utf-8"), original)
        self.assertEqual(self.page.presets[self.page.managed_preset_id("shared")]["ownership"], "app_managed")

    def test_collision_cancel_leaves_app_and_external_files_unchanged(self):
        managed = self.page.preset_store.save("shared", self.preset_data("1"))
        source = Path(self.temp_dir.name) / "shared.json"
        source.write_text(json.dumps(self.preset_data("2")), encoding="utf-8")
        self.page.load_presets_from_disk()

        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Cancel):
            self.import_file(source)

        self.assertEqual(json.loads(Path(managed["path"]).read_text())["config"]["Mass (kg)"], "1")
        self.assertEqual(len(self.page.presets), 1)

    def test_external_preset_exports_exact_loaded_json(self):
        source = Path(self.temp_dir.name) / "external.json"
        data = self.preset_data("42")
        source.write_text(json.dumps(data), encoding="utf-8")
        destination = Path(self.temp_dir.name) / "exports" / "copy.json"
        destination.parent.mkdir()

        self.import_file(source)
        preset_id = next(iter(self.page.presets))
        self.select_entry(preset_id)
        with patch.object(QFileDialog, "getSaveFileName", return_value=(str(destination), "JSON Files (*.json)")):
            self.page.export_preset()

        self.assertEqual(json.loads(destination.read_text(encoding="utf-8")), data)
        self.assertEqual(json.loads(source.read_text(encoding="utf-8")), data)

    def test_invalid_import_and_export_failure_preserve_state(self):
        invalid = Path(self.temp_dir.name) / "invalid.json"
        invalid.write_text("not json", encoding="utf-8")
        with patch.object(QMessageBox, "critical"):
            self.import_file(invalid)
        self.assertEqual(self.page.presets, {})

        with patch.object(QMessageBox, "critical"):
            self.import_file(Path(self.temp_dir.name) / "missing.json")
        self.assertEqual(self.page.presets, {})

        source = Path(self.temp_dir.name) / "external.json"
        source.write_text(json.dumps(self.preset_data()), encoding="utf-8")
        self.import_file(source)
        preset_id = next(iter(self.page.presets))
        self.select_entry(preset_id)
        with patch.object(QFileDialog, "getSaveFileName", return_value=("/unwritable/copy.json", "JSON Files (*.json)")), \
             patch.object(QMessageBox, "critical") as error:
            self.page.export_preset()
        error.assert_called_once()
        self.assertIn(preset_id, self.page.presets)

    def test_export_requires_selection_and_cancel_does_not_write(self):
        self.assertFalse(self.page.export_preset_btn.isEnabled())
        source = Path(self.temp_dir.name) / "external.json"
        source.write_text(json.dumps(self.preset_data()), encoding="utf-8")
        self.import_file(source)
        self.select_entry(next(iter(self.page.presets)))
        self.assertTrue(self.page.export_preset_btn.isEnabled())

        with patch.object(QFileDialog, "getSaveFileName", return_value=("", "")):
            self.page.export_preset()
        self.assertEqual(list(Path(self.temp_dir.name).glob("*.json")), [source])


class TransposePresetExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.user_data_root = Path(self.temp_dir.name) / "user-data"
        self.legacy_root = Path(self.temp_dir.name) / "legacy-resources"
        self.resource_patch = patch(
            "pages.transpose_page.resource_path",
            lambda relative: str(self.legacy_root / relative),
        )
        self.app_data_patch = patch(
            "pages.transpose_page.app_data_path",
            lambda relative: str(self.user_data_root / relative),
        )
        self.resource_patch.start()
        self.app_data_patch.start()
        self.page = TransposePage()

    def tearDown(self):
        self.page.close()
        self.app_data_patch.stop()
        self.resource_patch.stop()
        self.temp_dir.cleanup()

    def test_selected_airfield_preset_exports_without_changing_source(self):
        self.assertEqual(Path(self.page.presets_dir), self.user_data_root / "airfields")
        self.assertNotEqual(Path(self.page.presets_dir), self.legacy_root / "data" / "airfields")
        data = {
            "name": "Export Field",
            "latitude": "51.0",
            "longitude": "-1.0",
            "heading": "90",
            "original_elevation_m": "120",
        }
        entry = self.page.preset_store.save("export-field", data)
        self.page.load_presets_from_disk()
        self.assertFalse(self.page.export_preset_btn.isEnabled())
        self.page.preset_list.setCurrentRow(0)
        self.assertTrue(self.page.export_preset_btn.isEnabled())
        destination = Path(self.temp_dir.name) / "airfield-copy.json"

        with patch.object(QFileDialog, "getSaveFileName", return_value=(str(destination), "JSON Files (*.json)")):
            self.page.export_preset()

        self.assertEqual(json.loads(destination.read_text(encoding="utf-8")), data)
        self.assertEqual(json.loads(Path(entry["path"]).read_text(encoding="utf-8")), data)


class PresetStorageMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_app_data_path_uses_qt_location_not_bundle_resources(self):
        app_data = self.root / "application-data"
        with patch.object(resource_paths.QStandardPaths, "writableLocation", return_value=str(app_data)):
            resolved = resource_paths.app_data_path("debris-presets")

        self.assertEqual(Path(resolved), app_data / "debris-presets")
        self.assertNotIn("_MEIPASS", resolved)

    def test_migration_copies_valid_files_and_preserves_existing_destination(self):
        legacy = self.root / "legacy"
        destination = self.root / "user-data"
        legacy.mkdir()
        destination.mkdir()
        copied_bytes = b'{\n  "name": "Legacy"\n}\n'
        existing_bytes = b'{"name": "User version"}'
        (legacy / "copied.json").write_bytes(copied_bytes)
        (legacy / "existing.json").write_text('{"name": "Legacy version"}', encoding="utf-8")
        (legacy / "invalid.json").write_text("not json", encoding="utf-8")
        (destination / "existing.json").write_bytes(existing_bytes)

        PresetStore(destination, legacy)
        PresetStore(destination, legacy)

        self.assertEqual((destination / "copied.json").read_bytes(), copied_bytes)
        self.assertEqual((legacy / "copied.json").read_bytes(), copied_bytes)
        self.assertEqual((destination / "existing.json").read_bytes(), existing_bytes)
        self.assertFalse((destination / "invalid.json").exists())

    def test_migration_read_failure_is_silent(self):
        legacy = self.root / "legacy"
        destination = self.root / "user-data"
        legacy.mkdir()
        source = legacy / "unreadable.json"
        source.write_text('{"name": "Unreadable"}', encoding="utf-8")

        original_read_bytes = Path.read_bytes

        def fail_for_source(path):
            if path == source:
                raise OSError("cannot read")
            return original_read_bytes(path)

        with patch.object(Path, "read_bytes", fail_for_source):
            PresetStore(destination, legacy)

        self.assertFalse((destination / source.name).exists())

    def test_user_preset_survives_store_restart(self):
        destination = self.root / "user-data"
        data = {"name": "Persistent"}
        PresetStore(destination).save("persistent", data)

        restarted_store = PresetStore(destination)

        self.assertEqual(restarted_store.load_all()["persistent"]["data"], data)


if __name__ == "__main__":
    unittest.main()
