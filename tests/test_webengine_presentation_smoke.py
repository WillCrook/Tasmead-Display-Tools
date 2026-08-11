from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path


PRESENTATION_SMOKE_ENABLED = (
    os.environ.get("TASMEAD_WEBENGINE_PRESENTATION_SMOKE") == "1"
)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

if PRESENTATION_SMOKE_ENABLED:
    from webengine_runtime import configure_webengine_runtime

    configure_webengine_runtime()

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QCoreApplication, QEvent, QEventLoop, QTimer
from PyQt6.QtTest import QSignalSpy
from PyQt6.QtWidgets import QApplication

try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView
except ImportError:  # pragma: no cover - optional dependency path
    QWebEngineView = None


@unittest.skipUnless(
    PRESENTATION_SMOKE_ENABLED and QWebEngineView is not None,
    "requires opt-in native Qt WebEngine presentation test",
)
class WebEnginePresentationSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["tasmead-webengine-presentation-smoke"]
        )

    @staticmethod
    def _wait(milliseconds: int) -> None:
        loop = QEventLoop()
        QTimer.singleShot(milliseconds, loop.quit)
        loop.exec()

    @staticmethod
    def _run_javascript(view, source: str):
        results = []
        loop = QEventLoop()
        timeout = QTimer()
        timeout.setSingleShot(True)
        timeout.timeout.connect(loop.quit)
        timeout.start(5_000)
        view.page().runJavaScript(
            source,
            lambda value: (results.append(value), loop.quit()),
        )
        if not results:
            loop.exec()
        timeout.stop()
        if not results:
            raise AssertionError("JavaScript callback timed out.")
        return results[0]

    def test_webgl_frames_present_without_a_widget_resize(self):
        view = QWebEngineView()
        view.resize(320, 240)
        view.show()
        loaded = QSignalSpy(view.loadFinished)
        view.setHtml(
            """<!doctype html>
            <html><head><style>
            html,body,canvas { width:100%; height:100%; margin:0; display:block; }
            </style></head><body><canvas id="canvas" width="320" height="240"></canvas>
            <script>
            const gl = document.getElementById('canvas').getContext('webgl');
            window.paint = (red, green, blue) => {
              if (!gl) return false;
              gl.clearColor(red, green, blue, 1);
              gl.clear(gl.COLOR_BUFFER_BIT);
              gl.finish();
              return true;
            };
            window.paint(1, 0, 0);
            </script></body></html>"""
        )
        try:
            if len(loaded) == 0:
                self.assertTrue(loaded.wait(5_000))
            self.assertTrue(loaded[0][0])
            self._wait(250)
            first_image = view.grab().toImage()
            first = first_image.pixelColor(
                first_image.width() // 2,
                first_image.height() // 2,
            )
            self.assertGreater(first.red(), first.green() + 50)

            self.assertTrue(
                self._run_javascript(view, "window.paint(0, 1, 0);")
            )
            self._wait(250)
            second_image = view.grab().toImage()
            second = second_image.pixelColor(
                second_image.width() // 2,
                second_image.height() // 2,
            )

            self.assertGreater(second.green(), second.red() + 50)
            self.assertNotEqual(first.rgba(), second.rgba())
        finally:
            view.close()
            view.deleteLater()
            QCoreApplication.sendPostedEvents(
                None,
                QEvent.Type.DeferredDelete,
            )
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
