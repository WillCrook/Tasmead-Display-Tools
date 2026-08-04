import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from file_dialog_state import (
    FileDialogDirection,
    FileDialogWorkflow,
    ensure_extension,
    remember_directory,
    remember_file_selection,
    remembered_directory,
    suggested_save_path,
)


class FakeSettings:
    values = {}
    sync_count = 0

    def value(self, key, default=None, type=None):
        value = self.values.get(key, default)
        return type(value) if type is not None else value

    def setValue(self, key, value):
        self.values[key] = value

    def sync(self):
        type(self).sync_count += 1


class FileDialogStateTests(unittest.TestCase):
    def setUp(self):
        FakeSettings.values = {}
        FakeSettings.sync_count = 0
        self.settings_patch = patch("file_dialog_state.QSettings", FakeSettings)
        self.settings_patch.start()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()
        self.settings_patch.stop()

    def test_workflows_and_directions_remain_independent(self):
        transpose_input = self.root / "transpose-input"
        transpose_output = self.root / "transpose-output"
        debris_input = self.root / "debris-input"
        for directory in (transpose_input, transpose_output, debris_input):
            directory.mkdir()

        remember_file_selection(
            FileDialogWorkflow.TRANSPOSITION,
            FileDialogDirection.INPUT,
            transpose_input / "route.kml",
        )
        remember_directory(
            FileDialogWorkflow.TRANSPOSITION,
            FileDialogDirection.OUTPUT,
            transpose_output,
        )
        remember_file_selection(
            FileDialogWorkflow.DEBRIS,
            FileDialogDirection.INPUT,
            debris_input / "track.kml",
        )

        self.assertEqual(
            remembered_directory(
                FileDialogWorkflow.TRANSPOSITION,
                FileDialogDirection.INPUT,
            ),
            str(transpose_input),
        )
        self.assertEqual(
            remembered_directory(
                FileDialogWorkflow.TRANSPOSITION,
                FileDialogDirection.OUTPUT,
            ),
            str(transpose_output),
        )
        self.assertEqual(
            remembered_directory(
                FileDialogWorkflow.DEBRIS,
                FileDialogDirection.INPUT,
            ),
            str(debris_input),
        )
        self.assertEqual(
            suggested_save_path(
                FileDialogWorkflow.TRANSPOSITION,
                "editable-name.kml",
            ),
            str(transpose_output / "editable-name.kml"),
        )

    def test_missing_saved_directory_falls_back_without_reusing_other_workflow(self):
        fallback = self.root / "fallback"
        fallback.mkdir()
        FakeSettings.values[
            "file-dialogs/debris/input-directory"
        ] = str(self.root / "missing")

        with patch("file_dialog_state._default_directory", return_value=str(fallback)):
            selected = remembered_directory(
                FileDialogWorkflow.DEBRIS,
                FileDialogDirection.INPUT,
            )

        self.assertEqual(selected, str(fallback))

    def test_legacy_transposition_output_directory_is_migrated(self):
        legacy = self.root / "legacy-output"
        legacy.mkdir()
        FakeSettings.values["transpose/last-output-directory"] = str(legacy)

        selected = remembered_directory(
            FileDialogWorkflow.TRANSPOSITION,
            FileDialogDirection.OUTPUT,
        )

        self.assertEqual(selected, str(legacy))
        self.assertEqual(
            FakeSettings.values["file-dialogs/transposition/output-directory"],
            str(legacy),
        )
        self.assertEqual(FakeSettings.sync_count, 1)

    def test_extension_is_appended_without_replacing_custom_filename(self):
        self.assertEqual(ensure_extension("custom.name", ".kml"), "custom.name.kml")
        self.assertEqual(ensure_extension("custom.KML", ".kml"), "custom.KML")


if __name__ == "__main__":
    unittest.main()
