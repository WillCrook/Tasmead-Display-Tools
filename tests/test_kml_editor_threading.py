"""Subprocess regression coverage for pyproj work in the KML editor."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class KmlEditorThreadingRegressionTests(unittest.TestCase):
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
