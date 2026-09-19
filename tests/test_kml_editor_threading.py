"""Subprocess crash-regression coverage for KML editor background work."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class KmlEditorThreadingRegressionTests(unittest.TestCase):
    def test_crop_apply_then_timer_preview_does_not_abort(self):
        script = textwrap.dedent(
            """
            import tempfile
            from pathlib import Path

            from PyQt6.QtCore import QCoreApplication, QTimer

            from services.kml_editor_workspace import KmlEditorWorkspaceModel

            source = '''<?xml version="1.0" encoding="UTF-8"?>
            <kml xmlns="http://www.opengis.net/kml/2.2"
                 xmlns:gx="http://www.google.com/kml/ext/2.2"><Placemark><gx:Track>
            <when>2026-01-01T00:00:00Z</when>
            <when>2026-01-01T00:00:01Z</when>
            <when>2026-01-01T00:00:02Z</when>
            <gx:coord>-1 51 10</gx:coord>
            <gx:coord>-1.1 51.1 20</gx:coord>
            <gx:coord>-1.2 51.2 30</gx:coord>
            </gx:Track></Placemark></kml>'''
            app = QCoreApplication([])
            model = KmlEditorWorkspaceModel(validation_debounce_ms=0)
            state = {"applied": False}

            def validation_finished(document_id, _revision):
                document = model.document(document_id)
                if not state["applied"]:
                    state["applied"] = True
                    model.update_crop(document_id, 1, 2)
                    assert model.apply_crop(document_id)
                    return
                assert document.parse_state.point_count == 2
                assert (
                    document.crop_state.start_index,
                    document.crop_state.end_index,
                ) == (0, 1)
                QTimer.singleShot(
                    0,
                    lambda: model.request_crop_preview(document_id),
                )

            def operation_finished(_document_id, kind, purpose):
                if kind == "crop" and purpose == "preview":
                    print("crop timer preview completed")
                    model.shutdown()
                    app.quit()

            model.validation_finished.connect(validation_finished)
            model.operation_finished.connect(operation_finished)
            QTimer.singleShot(5000, lambda: app.exit(2))
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "crop-timer-regression.kml"
                path.write_text(source, encoding="utf-8")
                model.add_paths([path])
                exit_code = app.exec()
            model.shutdown()
            raise SystemExit(exit_code)
            """
        )
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(PROJECT_ROOT / "src")
        environment.setdefault("QT_QPA_PLATFORM", "offscreen")
        completed = subprocess.run(
            [sys.executable, "-X", "faulthandler", "-c", script],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(
            completed.returncode,
            0,
            msg=f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )
        self.assertIn("crop timer preview completed", completed.stdout)

    def test_repeated_real_simplification_does_not_crash_native_proj(self):
        script = textwrap.dedent(
            """
            import tempfile
            import time
            from pathlib import Path

            from PyQt6.QtCore import QCoreApplication

            from services.kml_editor_workspace import (
                KmlEditorWorkspaceModel,
                OperationStatus,
                ParseStatus,
            )

            source = '''<?xml version="1.0" encoding="UTF-8"?>
            <kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><LineString>
            <coordinates>-1,51,10 -1.1,51.1,20 -1.2,51.2,30</coordinates>
            </LineString></Placemark></kml>'''
            app = QCoreApplication([])
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "threading-regression.kml"
                path.write_text(source, encoding="utf-8")
                model = KmlEditorWorkspaceModel(validation_debounce_ms=0)
                document_id = model.add_paths([path]).document_ids[0]
                deadline = time.monotonic() + 5.0
                while (
                    model.document(document_id).parse_state.status
                    in {ParseStatus.STALE, ParseStatus.VALIDATING}
                    and time.monotonic() < deadline
                ):
                    app.processEvents()
                    time.sleep(0.001)
                assert model.document(document_id).parse_state.status == ParseStatus.VALID

                for index in range(250):
                    model.update_simplification_tolerance(
                        document_id,
                        1.0 + index % 20,
                    )
                    assert model.request_simplification(document_id)
                    assert model._operation_executor.wait_for_done(5.0)
                    app.processEvents()
                    assert (
                        model.document(document_id).simplification_state.status
                        == OperationStatus.READY
                    )
                model.shutdown()
            print("completed 250 simplifications")
            """
        )
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(PROJECT_ROOT / "src")
        environment.setdefault("QT_QPA_PLATFORM", "offscreen")
        completed = subprocess.run(
            [sys.executable, "-X", "faulthandler", "-c", script],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        self.assertEqual(
            completed.returncode,
            0,
            msg=f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )
        self.assertIn("completed 250 simplifications", completed.stdout)


if __name__ == "__main__":
    unittest.main()
