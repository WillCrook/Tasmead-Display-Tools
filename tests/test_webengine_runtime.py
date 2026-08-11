from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from webengine_runtime import select_scene_graph_backend


class WebEngineRuntimeTests(unittest.TestCase):
    def test_macos_defaults_to_opengl(self):
        environment = {}

        select_scene_graph_backend(platform="darwin", environ=environment)

        self.assertEqual(environment["QSG_RHI_BACKEND"], "opengl")

    def test_macos_preserves_an_explicit_backend(self):
        environment = {"QSG_RHI_BACKEND": "metal"}

        select_scene_graph_backend(platform="darwin", environ=environment)

        self.assertEqual(environment["QSG_RHI_BACKEND"], "metal")

    def test_other_platforms_keep_qt_defaults(self):
        for platform in ("win32", "linux"):
            with self.subTest(platform=platform):
                environment = {}
                select_scene_graph_backend(
                    platform=platform,
                    environ=environment,
                )
                self.assertNotIn("QSG_RHI_BACKEND", environment)

    def test_main_configures_shared_contexts_without_enabling_devtools(self):
        probe = (
            "import json, os, sys; "
            f"sys.path.insert(0, {str(PROJECT_ROOT / 'src')!r}); "
            "import main; "
            "from PyQt6.QtCore import QCoreApplication, Qt; "
            "print(json.dumps({"
            "'shared': QCoreApplication.testAttribute("
            "Qt.ApplicationAttribute.AA_ShareOpenGLContexts), "
            "'debug': os.environ.get('QTWEBENGINE_REMOTE_DEBUGGING')}))"
        )
        environment = os.environ.copy()
        environment.pop("QTWEBENGINE_REMOTE_DEBUGGING", None)
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=PROJECT_ROOT,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )

        result = json.loads(completed.stdout.strip())
        self.assertTrue(result["shared"])
        self.assertIsNone(result["debug"])

    def test_main_preserves_externally_enabled_devtools(self):
        probe = (
            "import os, sys; "
            f"sys.path.insert(0, {str(PROJECT_ROOT / 'src')!r}); "
            "import main; "
            "print(os.environ['QTWEBENGINE_REMOTE_DEBUGGING'])"
        )
        environment = os.environ.copy()
        environment["QTWEBENGINE_REMOTE_DEBUGGING"] = "9333"
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=PROJECT_ROOT,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.stdout.strip(), "9333")


if __name__ == "__main__":
    unittest.main()
