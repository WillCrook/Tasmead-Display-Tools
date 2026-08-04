import base64
import errno
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
KML_FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "kml"

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
)

from pages.debris_page import DebrisPage
from pages.transpose_page import TransposePage
import resource_paths
from services import (
    CURRENT_FORMAT_VERSION,
    Preset,
    PresetDestinationExistsError,
    PresetImportExportService,
    PresetIOError,
    PresetMalformedJsonError,
    PresetNameConflictError,
    PresetNameError,
    PresetRepository,
    PresetType,
    PresetTypeMismatchError,
    PresetValidationError,
    TranspositionBatchResult,
    TranspositionError,
    TranspositionErrorCode,
    TranspositionFileOutcome,
    TranspositionFileStatus,
    UnsupportedPresetVersionError,
    canonical_filename,
    canonical_stem,
    create_transposition_plan,
    readable_export_filename,
)


def write_preset(path: Path, preset: Preset) -> None:
    path.write_text(json.dumps(preset.to_dict()), encoding="utf-8")


class PresetFilenameTests(unittest.TestCase):
    def test_readable_canonical_filename_and_numbered_collisions(self):
        self.assertEqual(canonical_stem("Farnborough Runway 24"), "farnborough-runway-24")
        self.assertEqual(
            canonical_filename("Farnborough Runway 24"),
            "farnborough-runway-24.json",
        )
        self.assertEqual(
            canonical_filename(
                "Farnborough Runway 24",
                {"FARNBOROUGH-RUNWAY-24.JSON", "farnborough-runway-24-2.json"},
            ),
            "farnborough-runway-24-3.json",
        )

    def test_unicode_reserved_and_empty_ascii_names_are_safe(self):
        self.assertEqual(canonical_stem("Café"), "cafe")
        self.assertEqual(canonical_stem("CON"), "preset-con")
        self.assertEqual(canonical_stem("東京"), "preset")
        self.assertNotIn("/", readable_export_filename("Readable Name"))

    def test_path_like_and_control_names_are_rejected(self):
        for name in ("../preset", "folder/preset", "folder\\preset", "bad\x00name"):
            with self.subTest(name=repr(name)), self.assertRaises(PresetNameError):
                Preset.create(PresetType.DEBRIS, name, {})


class PresetLocationTests(unittest.TestCase):
    def test_app_data_path_uses_qt_writable_location(self):
        root = Path("/application-data")
        with patch.object(
            resource_paths.QStandardPaths,
            "writableLocation",
            return_value=str(root),
        ):
            resolved = resource_paths.app_data_path("presets/debris")

        self.assertEqual(Path(resolved), root / "presets" / "debris")


class PresetModelTests(unittest.TestCase):
    def test_complete_envelope_round_trips_and_rename_keeps_uuid(self):
        preset = Preset.create(PresetType.AIRFIELD, "Field", {"latitude": "51"})
        restored = Preset.from_dict(preset.to_dict(), expected_type=PresetType.AIRFIELD)
        renamed = restored.renamed("New Field")

        self.assertEqual(restored, preset)
        self.assertEqual(renamed.id, preset.id)
        self.assertEqual(renamed.name, "New Field")
        self.assertEqual(set(preset.to_dict()), {"formatVersion", "presetType", "id", "name", "data"})

    def test_schema_version_type_uuid_and_extra_fields_are_validated(self):
        document = Preset.create(PresetType.AIRFIELD, "Field", {}).to_dict()
        bad_documents = []
        for key in document:
            bad_documents.append({k: v for k, v in document.items() if k != key})
        for bad in bad_documents:
            with self.subTest(fields=bad.keys()), self.assertRaises(PresetValidationError):
                Preset.from_dict(bad)

        with self.assertRaises(UnsupportedPresetVersionError):
            Preset.from_dict({**document, "formatVersion": 2})
        with self.assertRaises(PresetTypeMismatchError):
            Preset.from_dict(document, expected_type=PresetType.DEBRIS)
        with self.assertRaises(PresetValidationError):
            Preset.from_dict({**document, "id": "not-a-uuid"})
        with self.assertRaises(PresetValidationError):
            Preset.from_dict({**document, "extra": True})
        with self.assertRaises(PresetValidationError):
            Preset.create(PresetType.AIRFIELD, "Invalid Data", {"value": float("nan")})


class PresetRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.repository = PresetRepository(self.root / "presets", PresetType.AIRFIELD)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_create_uses_uuid_identity_and_complete_managed_document(self):
        record = self.repository.create("Farnborough Runway 24", {"heading": "24"})
        document = json.loads(record.path.read_text(encoding="utf-8"))

        self.assertEqual(record.filename, "farnborough-runway-24.json")
        self.assertEqual(UUID(document["id"]), record.preset.id)
        self.assertEqual(document["formatVersion"], CURRENT_FORMAT_VERSION)
        self.assertEqual(document["name"], "Farnborough Runway 24")
        self.assertEqual(document["data"], {"heading": "24"})
        self.assertEqual(set(self.repository.load_all()), {record.preset.id})

    def test_filename_slug_collisions_are_numbered_without_name_identity(self):
        first = self.repository.create("Café", {"value": 1})
        second = self.repository.create("Cafe", {"value": 2})

        self.assertEqual(first.filename, "cafe.json")
        self.assertEqual(second.filename, "cafe-2.json")
        self.assertNotEqual(first.preset.id, second.preset.id)

        updated = self.repository.update_data(second.preset.id, {"value": 3})
        self.assertEqual(updated.filename, "cafe-2.json")
        self.assertEqual(updated.preset.data, {"value": 3})

    def test_names_are_unique_but_update_preserves_uuid(self):
        record = self.repository.create("Saved", {"value": 1})
        with self.assertRaises(PresetNameConflictError):
            self.repository.create("saved", {"value": 2})

        updated = self.repository.update_data(record.preset.id, {"value": 3})
        self.assertEqual(updated.preset.id, record.preset.id)
        self.assertEqual(updated.preset.data, {"value": 3})

    def test_rename_moves_file_and_preserves_uuid_and_data(self):
        record = self.repository.create("Old Name", {"value": 1})
        renamed = self.repository.rename(record.preset.id, "New Name")

        self.assertEqual(renamed.preset.id, record.preset.id)
        self.assertEqual(renamed.preset.data, record.preset.data)
        self.assertEqual(renamed.filename, "new-name.json")
        self.assertFalse(record.path.exists())
        self.assertTrue(renamed.path.exists())

    def test_delete_resolves_by_uuid_and_never_uses_external_paths(self):
        record = self.repository.create("Delete Me", {})
        external = self.root / "external.json"
        external.write_text("{}", encoding="utf-8")

        self.repository.delete(record.preset.id)

        self.assertFalse(record.path.exists())
        self.assertTrue(external.exists())

    def test_create_only_write_falls_back_when_hardlinks_are_unsupported(self):
        with patch("services.preset_store.os.link", side_effect=OSError(errno.ENOTSUP, "no links")):
            record = self.repository.create("Portable", {"value": 1})

        self.assertEqual(json.loads(record.path.read_text())["name"], "Portable")
        self.assertEqual(list(record.path.parent.glob(".preset-*.tmp")), [])

    def test_unwritable_repository_does_not_crash_construction(self):
        with patch.object(Path, "mkdir", side_effect=PermissionError("denied")):
            repository = PresetRepository(self.root / "unwritable", PresetType.AIRFIELD)

        self.assertEqual(repository.load_all(), {})
        self.assertTrue(any("Cannot create preset directory" in issue for issue in repository.issues))
        with self.assertRaises(PresetIOError):
            repository.create("Cannot Save", {})


class PresetMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.managed = self.root / "old-managed"
        self.bundled = self.root / "bundled"
        self.destination = self.root / "presets" / "airfield"
        self.backup = self.root / "presets" / "legacy-backup" / "airfield"
        self.managed.mkdir(parents=True)
        self.bundled.mkdir(parents=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def repository(self):
        return PresetRepository(
            self.destination,
            PresetType.AIRFIELD,
            legacy_managed_directories=(self.managed,),
            legacy_readonly_directories=(self.bundled,),
            backup_directory=self.backup,
        )

    def test_plain_and_base64_legacy_files_are_wrapped_and_backed_up(self):
        (self.managed / "Farnborough.json").write_text(
            json.dumps({"heading": "24"}), encoding="utf-8"
        )
        encoded = base64.urlsafe_b64encode("Café".encode()).decode().rstrip("=")
        (self.managed / f"preset-v1-{encoded}.json").write_text(
            json.dumps({"heading": "06"}), encoding="utf-8"
        )

        repository = self.repository()
        records = repository.load_all()

        self.assertEqual({record.preset.name for record in records.values()}, {"Farnborough", "Café"})
        self.assertTrue(all(record.preset.format_version == 1 for record in records.values()))
        self.assertEqual(len(list(self.backup.glob("*.json"))), 2)
        self.assertEqual(list(self.managed.glob("*.json")), [])

    def test_bundled_migration_is_idempotent_and_preserves_source(self):
        source = self.bundled / "Bundled Field.json"
        source.write_text(json.dumps({"heading": "18"}), encoding="utf-8")

        first = self.repository()
        first_ids = set(first.load_all())
        second = self.repository()

        self.assertEqual(set(second.load_all()), first_ids)
        self.assertTrue(source.exists())
        self.assertEqual(len(first_ids), 1)

    def test_malformed_legacy_file_is_reported_and_left_untouched(self):
        source = self.managed / "broken.json"
        source.write_text("not json", encoding="utf-8")

        repository = self.repository()

        self.assertTrue(source.exists())
        self.assertTrue(any("Malformed legacy preset" in issue for issue in repository.issues))
        self.assertEqual(repository.load_all(), {})


class PresetTransferTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.repository = PresetRepository(self.root / "managed", PresetType.DEBRIS)
        self.transfer = PresetImportExportService(self.repository)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_import_uses_metadata_not_arbitrary_filename_and_copies_source(self):
        source = self.root / "anything at all.preset"
        preset = Preset.create(PresetType.DEBRIS, "Metadata Name", {"mass": 10})
        write_preset(source, preset)
        original = source.read_bytes()

        inspection = self.transfer.inspect_import(source)
        record = self.transfer.import_new(inspection.preset)

        self.assertEqual(record.preset.name, "Metadata Name")
        self.assertEqual(record.filename, "metadata-name.json")
        self.assertEqual(source.read_bytes(), original)
        self.assertNotEqual(record.path, source)

    def test_duplicate_uuid_replace_copy_and_cancel_outcomes(self):
        existing = self.repository.create("Original", {"value": 1})
        imported = Preset.create(
            PresetType.DEBRIS,
            "Imported",
            {"value": 2},
            preset_id=existing.preset.id,
        )
        source = self.root / "duplicate.weird.json"
        write_preset(source, imported)
        inspection = self.transfer.inspect_import(source)
        self.assertEqual(inspection.existing.preset.id, existing.preset.id)

        # Cancel is represented by not committing the inspected candidate.
        self.assertEqual(self.repository.get(existing.preset.id).preset.data, {"value": 1})
        replaced = self.transfer.replace(imported)
        self.assertEqual(replaced.preset.id, existing.preset.id)
        self.assertEqual(replaced.preset.name, "Imported")
        copied = self.transfer.import_copy(imported, name="Imported Copy")
        self.assertNotEqual(copied.preset.id, imported.id)
        self.assertEqual(copied.preset.data, imported.data)

    def test_malformed_missing_unsupported_and_wrong_type_imports_fail_cleanly(self):
        malformed = self.root / "malformed.json"
        malformed.write_text("not json", encoding="utf-8")
        with self.assertRaises(PresetMalformedJsonError):
            self.transfer.inspect_import(malformed)
        nonstandard = self.root / "nan.json"
        nonstandard.write_text('{"value": NaN}', encoding="utf-8")
        with self.assertRaises(PresetMalformedJsonError):
            self.transfer.inspect_import(nonstandard)

        valid = Preset.create(PresetType.DEBRIS, "Valid", {}).to_dict()
        cases = [
            ({"name": "raw legacy"}, PresetValidationError),
            ({**valid, "formatVersion": 99}, UnsupportedPresetVersionError),
            (Preset.create(PresetType.AIRFIELD, "Wrong", {}).to_dict(), PresetTypeMismatchError),
        ]
        for index, (document, exception) in enumerate(cases):
            path = self.root / f"invalid-{index}.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.subTest(index=index), self.assertRaises(exception):
                self.transfer.inspect_import(path)

    def test_export_uses_full_envelope_arbitrary_name_and_explicit_overwrite(self):
        record = self.repository.create("Readable Name", {"value": 7})
        destination = self.root / "my chosen filename.data.json"
        self.transfer.export(record.preset, destination)

        self.assertEqual(json.loads(destination.read_text()), record.preset.to_dict())
        self.assertEqual(
            self.transfer.suggested_export_filename(record.preset),
            "Readable Name.json",
        )
        with self.assertRaises(PresetDestinationExistsError):
            self.transfer.export(record.preset, destination)
        self.transfer.export(record.preset.with_data({"value": 8}), destination, overwrite=True)
        self.assertEqual(json.loads(destination.read_text())["data"], {"value": 8})


class PresetPageTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.app_root = self.root / "app-data"
        self.bundle_root = self.root / "bundle"

    def tearDown(self):
        self.temp_dir.cleanup()

    def app_data_path(self, relative):
        return str(self.app_root / relative)

    def resource_path(self, relative):
        return str(self.bundle_root / relative)

    @staticmethod
    def select(page, preset_id):
        for row in range(page.preset_list.count()):
            item = page.preset_list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == str(preset_id):
                page.preset_list.setCurrentItem(item)
                return item
        raise AssertionError(f"Preset {preset_id} was not listed")


class DebrisPagePresetTests(PresetPageTestCase):
    def setUp(self):
        super().setUp()
        self.app_patch = patch("pages.debris_page.app_data_path", self.app_data_path)
        self.resource_patch = patch("pages.debris_page.resource_path", self.resource_path)
        self.app_patch.start()
        self.resource_patch.start()
        self.page = DebrisPage()

    def tearDown(self):
        self.page.close()
        self.resource_patch.stop()
        self.app_patch.stop()
        super().tearDown()

    @staticmethod
    def data(mass="10"):
        return {"config": {"Mass (kg)": mass}, "flight_mode": "kml"}

    @staticmethod
    def kml_data(path, altitude=""):
        return {
            "config": {"Mass (kg)": "10"},
            "altitude_m": altitude,
            "flight_mode": "kml",
            "flight_inputs": {"kml": {"kml_path": str(path)}},
        }

    def restore(self, name, data):
        record = self.page.preset_repository.create(name, data)
        self.page.load_presets_from_disk()
        item = self.select(self.page, record.preset.id)
        self.page.load_selected_preset(item)
        return record

    def test_uses_new_managed_root_and_uuid_item_data(self):
        record = self.page.preset_repository.create("Managed", self.data())
        self.page.load_presets_from_disk()
        item = self.select(self.page, record.preset.id)

        self.assertEqual(Path(self.page.presets_dir), self.app_root / "presets" / "debris")
        self.assertEqual(UUID(item.data(Qt.ItemDataRole.UserRole)), record.preset.id)
        self.assertTrue(self.page.rename_preset_btn.isEnabled())
        self.assertTrue(self.page.export_preset_btn.isEnabled())

    def test_preset_panel_keeps_exact_visible_text_and_order(self):
        labels = [
            label.text()
            for label in self.page.presets_widget.findChildren(QLabel)
        ]
        buttons = [
            button.text()
            for button in self.page.presets_widget.findChildren(QPushButton)
        ]

        self.assertIn("Presets", labels)
        self.assertEqual(
            buttons,
            [
                "Save preset",
                "Load preset",
                "Rename preset",
                "Delete preset",
                "Export preset",
            ],
        )

    def test_unit_fields_keep_existing_conversion_and_derived_rules(self):
        self.page.terrain_m.setText("20")
        self.page.alt_m.setText("100")
        self.assertEqual(self.page.terrain_ft.text(), "65.62")
        self.assertEqual(self.page.alt_ft.text(), "328.08")
        self.assertEqual(self.page.height_m.text(), "80.00")
        self.assertEqual(self.page.height_ft.text(), "262.47")

        self.page.height_ft.setText("164.042")
        self.assertEqual(self.page.height_m.text(), "50.00")
        self.assertEqual(self.page.alt_m.text(), "70.00")
        self.assertEqual(self.page.alt_ft.text(), "229.66")

    def test_capture_preset_data_keeps_exact_debris_schema(self):
        self.page.inputs["Mass (kg)"].setText("12")
        self.page.surface_combo.setCurrentText("grass")
        self.page.include_ground_drag.setChecked(False)
        self.page.terrain_m.setText("20")
        self.page.alt_m.setText("100")
        self.page.rb_bearing.setChecked(True)
        self.page.bearing_lat_input.setText("51")
        self.page.bearing_lon_input.setText("-1")
        self.page.azimuth_input.setText("90")

        data = self.page.capture_preset_data()

        self.assertEqual(
            set(data),
            {
                "config",
                "surface",
                "include_ground_drag",
                "altitude_m",
                "terrain_m",
                "height_m",
                "flight_mode",
                "flight_inputs",
            },
        )
        self.assertEqual(data["config"]["Mass (kg)"], "12")
        self.assertEqual(data["surface"], "grass")
        self.assertFalse(data["include_ground_drag"])
        self.assertEqual(data["altitude_m"], "100")
        self.assertEqual(data["terrain_m"], "20")
        self.assertEqual(data["height_m"], "80.00")
        self.assertEqual(data["flight_mode"], "bearing")
        self.assertEqual(
            data["flight_inputs"]["bearing"],
            {"lat": "51", "lon": "-1", "azimuth": "90"},
        )

    def test_apply_preset_data_keeps_coords_and_bearing_workflows_explicit(self):
        cases = (
            (
                "coords",
                {
                    "coords": {
                        "lat1": "51.1",
                        "lon1": "-1.1",
                        "lat2": "51.2",
                        "lon2": "-1.2",
                    }
                },
                ("51.1", "-1.1", "51.2", "-1.2"),
            ),
            (
                "bearing",
                {
                    "bearing": {
                        "lat": "52.1",
                        "lon": "-2.1",
                        "azimuth": "135",
                    }
                },
                ("52.1", "-2.1", "135"),
            ),
        )

        for mode, flight_inputs, expected in cases:
            with self.subTest(mode=mode):
                self.page.apply_preset_data(
                    {
                        "flight_mode": mode,
                        "flight_inputs": flight_inputs,
                    }
                )
                self.assertEqual(self.page.flight_mode, mode)
                if mode == "coords":
                    actual = (
                        self.page.lat1_input.text(),
                        self.page.lon1_input.text(),
                        self.page.lat2_input.text(),
                        self.page.lon2_input.text(),
                    )
                else:
                    actual = (
                        self.page.bearing_lat_input.text(),
                        self.page.bearing_lon_input.text(),
                        self.page.azimuth_input.text(),
                    )
                self.assertEqual(actual, expected)

    def test_restoring_kml_preset_parses_immediately_and_kml_altitude_wins(self):
        path = KML_FIXTURES / "line_string_namespaced.kml"

        self.restore("Three dimensional", self.kml_data(path, altitude="999"))

        self.assertTrue(self.page._kml_state.ready)
        self.assertEqual(self.page.kml_input_path, str(path))
        self.assertEqual(self.page._kml_state.coordinates, (51.2, -0.7, 51.3, -0.6))
        self.assertEqual(self.page.alt_m.text(), "125.0")
        self.assertEqual(self.page.kml_status_label.text(), "KML ready.")

    def test_restoring_two_dimensional_kml_uses_saved_manual_altitude(self):
        path = KML_FIXTURES / "line_string_namespace_free_2d.kml"
        with patch.object(QMessageBox, "warning") as warning:
            self.restore("Two dimensional", self.kml_data(path, altitude="350"))

        self.assertTrue(self.page._kml_state.ready)
        self.assertIsNone(self.page._kml_state.final_altitude_m)
        self.assertEqual(self.page.alt_m.text(), "350")
        self.assertIn("using entered altitude", self.page.kml_status_label.text())
        warning.assert_not_called()

    def test_restoring_empty_kml_path_clears_previous_selection(self):
        self.page.select_and_parse_kml(KML_FIXTURES / "line_string_namespaced.kml")

        self.restore("No KML", self.kml_data("", altitude="410"))

        self.assertFalse(self.page._kml_state.ready)
        self.assertEqual(self.page.kml_input_path, "")
        self.assertIsNone(self.page._kml_state.coordinates)
        self.assertEqual(self.page.file_label.text(), "Drop KML file here")
        self.assertEqual(self.page.alt_m.text(), "410")
        self.assertFalse(self.page.load_kml_btn.isEnabled())

    def test_restoring_invalid_kml_keeps_path_and_error_without_stale_coordinates(self):
        self.page.select_and_parse_kml(KML_FIXTURES / "line_string_namespaced.kml")
        invalid = KML_FIXTURES / "not-present.kml"
        with patch.object(QMessageBox, "critical") as critical:
            self.restore("Missing KML", self.kml_data(invalid, altitude="410"))

        self.assertFalse(self.page._kml_state.ready)
        self.assertEqual(self.page.kml_input_path, str(invalid))
        self.assertIsNone(self.page._kml_state.coordinates)
        self.assertTrue(self.page._kml_state.error)
        self.assertEqual(self.page.alt_m.text(), "410")
        self.assertIn("KML error:", self.page.kml_status_label.text())
        critical.assert_called_once()

    def test_duplicate_uuid_cancel_does_not_import_or_change_source(self):
        existing = self.page.preset_repository.create("Existing", self.data("1"))
        imported = Preset.create(
            PresetType.DEBRIS,
            "Imported",
            self.data("2"),
            preset_id=existing.preset.id,
        )
        source = self.root / "external-name.json"
        write_preset(source, imported)
        original = source.read_bytes()

        with patch.object(QFileDialog, "getOpenFileName", return_value=(str(source), "")), \
             patch.object(self.page, "choose_duplicate_uuid_action", return_value="cancel"):
            self.page.load_preset_from_file()

        stored = self.page.preset_repository.get(existing.preset.id)
        self.assertEqual(stored.preset.data, self.data("1"))
        self.assertEqual(source.read_bytes(), original)

    def test_duplicate_uuid_copy_gets_new_id_and_unique_name(self):
        existing = self.page.preset_repository.create("Shared", self.data("1"))
        imported = Preset.create(
            PresetType.DEBRIS,
            "Shared",
            self.data("2"),
            preset_id=existing.preset.id,
        )
        source = self.root / "copy-me.json"
        write_preset(source, imported)

        with patch.object(QFileDialog, "getOpenFileName", return_value=(str(source), "")), \
             patch.object(self.page, "choose_duplicate_uuid_action", return_value="copy"), \
             patch.object(QInputDialog, "getText", return_value=("Shared (2)", True)):
            self.page.load_preset_from_file()

        records = self.page.preset_repository.load_all()
        self.assertEqual(len(records), 2)
        copy_record = next(record for record in records.values() if record.preset.id != existing.preset.id)
        self.assertEqual(copy_record.preset.name, "Shared (2)")
        self.assertEqual(copy_record.preset.data, self.data("2"))

    def test_different_uuid_same_name_prompts_for_unique_managed_name(self):
        self.page.preset_repository.create("Shared", self.data("1"))
        imported = Preset.create(PresetType.DEBRIS, "Shared", self.data("2"))
        source = self.root / "same-name.preset"
        write_preset(source, imported)

        with patch.object(QFileDialog, "getOpenFileName", return_value=(str(source), "")), \
             patch.object(QInputDialog, "getText", return_value=("Shared (2)", True)):
            self.page.load_preset_from_file()

        imported_record = self.page.preset_repository.get(imported.id)
        self.assertIsNotNone(imported_record)
        self.assertEqual(imported_record.preset.name, "Shared (2)")
        self.assertEqual(imported_record.filename, "shared-2.json")

    def test_rename_and_export_keep_uuid_and_emit_full_metadata(self):
        record = self.page.preset_repository.create("Before", self.data())
        self.page.load_presets_from_disk()
        self.select(self.page, record.preset.id)
        with patch.object(QInputDialog, "getText", return_value=("After", True)):
            self.page.rename_preset()
        renamed = self.page.preset_repository.get(record.preset.id)

        destination = self.root / "Friendly export.json"
        self.select(self.page, record.preset.id)
        with patch.object(QFileDialog, "getSaveFileName", return_value=(str(destination), "")):
            self.page.export_preset()

        document = json.loads(destination.read_text())
        self.assertEqual(renamed.preset.id, record.preset.id)
        self.assertEqual(renamed.filename, "after.json")
        self.assertEqual(document["id"], str(record.preset.id))
        self.assertEqual(document["name"], "After")
        self.assertEqual(document["presetType"], "debris")


class TransposePagePresetTests(PresetPageTestCase):
    def setUp(self):
        super().setUp()
        self.app_patch = patch("pages.transpose_page.app_data_path", self.app_data_path)
        self.resource_patch = patch("pages.transpose_page.resource_path", self.resource_path)
        self.app_patch.start()
        self.resource_patch.start()
        self.page = TransposePage()

    def tearDown(self):
        self.page.close()
        self.resource_patch.stop()
        self.app_patch.stop()
        super().tearDown()

    @staticmethod
    def data(name="Farnborough"):
        return {
            "name": name,
            "latitude": "51.272833",
            "longitude": "-0.792044",
            "heading": "126",
            "original_elevation_m": "38",
        }

    def test_import_is_persisted_selected_and_applied(self):
        preset = Preset.create(PresetType.AIRFIELD, "Imported Field", self.data())
        source = self.root / "not-the-preset-name.json"
        write_preset(source, preset)

        with patch.object(QFileDialog, "getOpenFileName", return_value=(str(source), "")):
            self.page.load_preset_from_file()

        self.assertIsNotNone(self.page.preset_repository.get(preset.id))
        self.assertEqual(self.page.airfield_name_input.text(), "Farnborough")
        self.assertEqual(self.page.heading_input.text(), "126")
        self.assertEqual(self.page.preset_list.currentItem().text(), "Imported Field")

    def test_preset_panel_keeps_exact_visible_text_and_order(self):
        splitter = self.page.layout().itemAt(0).widget()
        presets_widget = splitter.widget(0)
        labels = [label.text() for label in presets_widget.findChildren(QLabel)]
        buttons = [
            button.text()
            for button in presets_widget.findChildren(QPushButton)
        ]

        self.assertIn("Airfield Presets", labels)
        self.assertEqual(
            buttons,
            [
                "Save Preset",
                "Load Preset",
                "Rename Preset",
                "Delete Preset",
                "Export Preset",
            ],
        )

    def test_elevation_pair_keeps_existing_conversion_behavior(self):
        self.page.orig_height_input.setText("10")
        self.assertEqual(self.page.orig_height_ft_input.text(), "32.81")

        self.page.orig_height_ft_input.setText("65.6168")
        self.assertEqual(self.page.orig_height_input.text(), "20.00")

        self.page.orig_height_input.setText("invalid")
        self.assertEqual(self.page.orig_height_ft_input.text(), "")

    def test_capture_and_apply_keep_exact_airfield_schema(self):
        expected = self.data()
        self.page.apply_preset_data(expected)

        self.assertEqual(self.page.capture_preset_data(), expected)

    def test_wrong_type_and_raw_legacy_external_imports_show_errors_without_writes(self):
        wrong = self.root / "wrong.json"
        raw = self.root / "raw.json"
        write_preset(wrong, Preset.create(PresetType.DEBRIS, "Wrong", {}))
        raw.write_text(json.dumps(self.data()), encoding="utf-8")

        for source in (wrong, raw):
            with self.subTest(source=source.name), \
                 patch.object(QFileDialog, "getOpenFileName", return_value=(str(source), "")), \
                 patch.object(QMessageBox, "critical") as error:
                self.page.load_preset_from_file()
                error.assert_called_once()
        self.assertEqual(self.page.preset_repository.load_all(), {})

    def test_same_name_save_requires_confirmation_and_preserves_uuid(self):
        existing = self.page.preset_repository.create("Shared", self.data("Original"))
        self.page.airfield_name_input.setText("Updated")
        with patch.object(QInputDialog, "getText", return_value=("Shared", True)), \
             patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.page.save_preset()

        updated = self.page.preset_repository.get(existing.preset.id)
        self.assertEqual(updated.preset.id, existing.preset.id)
        self.assertEqual(updated.preset.data["name"], "Updated")
        self.assertEqual(len(self.page.preset_repository.load_all()), 1)

    def configure_transposition(self, *input_paths, airfield_name="RAF Fairford"):
        self.page.input_files = [str(path) for path in input_paths]
        self.page.airfield_name_input.setText(airfield_name)
        self.page.lat_input.setText("51.0")
        self.page.lon_input.setText("-1.0")
        self.page.heading_input.setText("90")
        self.page.orig_height_input.setText("38")

    def test_transposition_uses_folder_preview_persists_location_and_runs_plan(self):
        source = KML_FIXTURES / "line_string_namespaced.kml"
        output_dir = self.root / "outputs"
        output_dir.mkdir()
        self.configure_transposition(source, airfield_name="")
        successful_output = TranspositionFileOutcome(
            input_path=source,
            planned_output_path=output_dir / "line-string-namespaced-at-airfield.kml",
            final_output_path=output_dir / "line-string-namespaced-at-airfield.kml",
            status=TranspositionFileStatus.SUCCEEDED,
        )
        result = TranspositionBatchResult(
            outcomes=(successful_output,),
        )

        with (
            patch.object(
                QFileDialog,
                "getExistingDirectory",
                return_value=str(output_dir),
            ) as folder_dialog,
            patch.object(self.page, "_initial_output_directory", return_value=str(self.root)),
            patch.object(self.page, "_confirm_output_plan", return_value=True) as confirm,
            patch("pages.transpose_page.run_transposition", return_value=result) as run,
            patch("pages.transpose_page.QSettings") as settings_class,
            patch.object(QMessageBox, "information") as information,
        ):
            self.page.run_transposition_ui()

        folder_dialog.assert_called_once_with(
            self.page,
            "Select Output Folder",
            str(self.root),
        )
        plan = confirm.call_args.args[0]
        self.assertEqual(
            plan.jobs[0].output_path.name,
            "line-string-namespaced-at-airfield.kml",
        )
        self.assertIs(run.call_args.kwargs["plan"], plan)
        settings_class.return_value.setValue.assert_called_once_with(
            self.page.LAST_OUTPUT_DIRECTORY_KEY,
            str(output_dir),
        )
        settings_class.return_value.sync.assert_called_once_with()
        information.assert_called_once()
        self.assertIn(
            str(successful_output.output_path),
            information.call_args.args[2],
        )

    def test_cancelled_preview_writes_nothing_and_does_not_persist_folder(self):
        source = KML_FIXTURES / "line_string_namespaced.kml"
        output_dir = self.root / "outputs"
        output_dir.mkdir()
        self.configure_transposition(source)

        with (
            patch.object(
                QFileDialog,
                "getExistingDirectory",
                return_value=str(output_dir),
            ),
            patch.object(self.page, "_confirm_output_plan", return_value=False),
            patch("pages.transpose_page.run_transposition") as run,
            patch("pages.transpose_page.QSettings") as settings_class,
        ):
            self.page.run_transposition_ui()

        run.assert_not_called()
        settings_class.return_value.setValue.assert_not_called()
        self.assertEqual(list(output_dir.iterdir()), [])

    def test_partial_failure_dialog_reports_every_success_and_failure(self):
        first = KML_FIXTURES / "line_string_namespaced.kml"
        failed = KML_FIXTURES / "wrong_arity.kml"
        last = KML_FIXTURES / "gx_track.kml"
        output_dir = self.root / "outputs"
        output_dir.mkdir()
        self.configure_transposition(first, failed, last)
        successful_output = TranspositionFileOutcome(
            input_path=first,
            planned_output_path=output_dir / "line-string-namespaced-at-raf-fairford.kml",
            final_output_path=output_dir / "line-string-namespaced-at-raf-fairford.kml",
            status=TranspositionFileStatus.SUCCEEDED,
        )
        last_output = TranspositionFileOutcome(
            input_path=last,
            planned_output_path=output_dir / "gx-track-at-raf-fairford.kml",
            final_output_path=output_dir / "gx-track-at-raf-fairford.kml",
            status=TranspositionFileStatus.SUCCEEDED,
        )
        failed_outcome = TranspositionFileOutcome(
            input_path=failed,
            planned_output_path=output_dir / "wrong-arity-at-raf-fairford.kml",
            final_output_path=None,
            status=TranspositionFileStatus.FAILED,
            error=TranspositionError(
                code=TranspositionErrorCode.INPUT_KML,
                message="invalid coordinate",
                input_path=failed,
                intended_output_path=output_dir / "wrong-arity-at-raf-fairford.kml",
                exception_type="KmlCoordinateError",
            ),
        )
        result = TranspositionBatchResult(
            outcomes=(successful_output, failed_outcome, last_output),
        )

        with (
            patch.object(
                QFileDialog,
                "getExistingDirectory",
                return_value=str(output_dir),
            ),
            patch.object(self.page, "_confirm_output_plan", return_value=True),
            patch("pages.transpose_page.run_transposition", return_value=result),
            patch("pages.transpose_page.QSettings"),
            patch.object(QMessageBox, "warning") as warning,
        ):
            self.page.run_transposition_ui()

        message = warning.call_args.args[2]
        self.assertIn("Saved 2 of 3", message)
        self.assertIn(str(successful_output.output_path), message)
        self.assertIn(str(failed), message)
        self.assertIn("invalid coordinate", message)

    def test_all_failed_result_uses_critical_dialog_not_success(self):
        source = KML_FIXTURES / "wrong_arity.kml"
        output_dir = self.root / "outputs"
        output_dir.mkdir()
        self.configure_transposition(source)
        failure = TranspositionFileOutcome(
            input_path=source,
            planned_output_path=output_dir / "wrong-arity-at-raf-fairford.kml",
            final_output_path=None,
            status=TranspositionFileStatus.FAILED,
            error=TranspositionError(
                code=TranspositionErrorCode.INPUT_KML,
                message="invalid coordinate",
                input_path=source,
                intended_output_path=output_dir / "wrong-arity-at-raf-fairford.kml",
                exception_type="KmlCoordinateError",
            ),
        )
        result = TranspositionBatchResult(outcomes=(failure,))

        with (
            patch.object(QFileDialog, "getExistingDirectory", return_value=str(output_dir)),
            patch.object(self.page, "_confirm_output_plan", return_value=True),
            patch("pages.transpose_page.run_transposition", return_value=result),
            patch("pages.transpose_page.QSettings"),
            patch.object(QMessageBox, "critical") as critical,
            patch.object(QMessageBox, "information") as information,
        ):
            self.page.run_transposition_ui()

        critical.assert_called_once()
        self.assertIn("No KML files were produced", critical.call_args.args[2])
        information.assert_not_called()

    def test_initial_output_directory_uses_existing_saved_location(self):
        output_dir = self.root / "remembered"
        output_dir.mkdir()

        with patch("pages.transpose_page.QSettings") as settings_class:
            settings_class.return_value.value.return_value = str(output_dir)
            initial = self.page._initial_output_directory()

        self.assertEqual(initial, str(output_dir))
        settings_class.return_value.value.assert_called_once_with(
            self.page.LAST_OUTPUT_DIRECTORY_KEY,
            "",
            type=str,
        )

    def test_confirmation_preview_lists_every_planned_filename(self):
        output_dir = self.root / "outputs"
        output_dir.mkdir()
        plan = create_transposition_plan(
            [
                KML_FIXTURES / "line_string_namespaced.kml",
                KML_FIXTURES / "gx_track.kml",
            ],
            output_dir,
            "RAF Fairford",
        )

        with (
            patch.object(
                QMessageBox,
                "exec",
                return_value=QMessageBox.StandardButton.Save,
            ),
            patch.object(QMessageBox, "setDetailedText") as detailed_text,
        ):
            accepted = self.page._confirm_output_plan(plan)

        self.assertTrue(accepted)
        preview = detailed_text.call_args.args[0]
        self.assertIn("line-string-namespaced-at-raf-fairford.kml", preview)
        self.assertIn("gx-track-at-raf-fairford.kml", preview)


if __name__ == "__main__":
    unittest.main()
