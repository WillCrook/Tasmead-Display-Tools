import os
import sys
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from PyQt6.QtCore import QPoint, QRect, QThread, QTimer, Qt
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtTest import QSignalSpy, QTest
from PyQt6.QtWidgets import QApplication, QMessageBox

from app_window import App
from pages.debris_page import DebrisPage, SimulationUiState
from services import (
    DebrisSimulationRequest,
    DebrisSimulationResult,
    SimulationCancelled,
    SimulationPhase,
    SimulationProgress,
    run_debris_simulation_request,
)
from workers import CancellationToken, DebrisSimulationWorker, SimulationFailure


def make_request(output_file, **overrides):
    values = {
        "mass_kg": 10.0,
        "area_m2": 0.1,
        "Cd": 0.5,
        "rho": 1.225,
        "g": 9.81,
        "dt": 0.01,
        "ktas": 50.0,
        "surface": "concrete",
        "slide_physics": 0.5,
        "include_ground_drag": True,
        "terrain_m": 0.0,
        "altitude_m": 20.0,
        "input_coords": None,
        "input_bearing": (51.0, -1.0, 90.0),
        "output_file": os.fspath(output_file),
    }
    values.update(overrides)
    return DebrisSimulationRequest(**values)


def make_result(output_file):
    return DebrisSimulationResult(
        heading=90.0,
        air_distance_m=10.0,
        ground_distance_m=2.0,
        total_distance_m=12.0,
        impacts=1,
        output_file=os.fspath(output_file),
    )


class DebrisSimulationServiceTests(unittest.TestCase):
    def test_request_is_frozen_and_success_reports_ordered_phases(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "trajectory.kml"
            output.write_text("existing", encoding="utf-8")
            request = make_request(output)
            with self.assertRaises(FrozenInstanceError):
                request.mass_kg = 20.0

            progress = []
            result = run_debris_simulation_request(
                request,
                progress_callback=progress.append,
            )

            self.assertEqual(result.output_file, str(output))
            self.assertTrue(output.read_text(encoding="utf-8").endswith("</kml>\n"))
            phases = [item.phase for item in progress]
            self.assertEqual(phases[0], SimulationPhase.SIMULATING)
            self.assertEqual(phases[-1], SimulationPhase.WRITING)
            self.assertLess(
                phases.index(SimulationPhase.SIMULATING),
                phases.index(SimulationPhase.PROJECTING),
            )
            self.assertLess(
                phases.index(SimulationPhase.PROJECTING),
                phases.index(SimulationPhase.WRITING),
            )
            for phase in SimulationPhase:
                phase_items = [item for item in progress if item.phase is phase]
                self.assertTrue(phase_items)
                self.assertEqual(phase_items[-1].completed, phase_items[-1].total)
                self.assertEqual(
                    [item.completed for item in phase_items],
                    sorted(item.completed for item in phase_items),
                )

    def test_cancellation_during_write_preserves_existing_destination(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "trajectory.kml"
            output.write_text("existing", encoding="utf-8")
            request = make_request(output, altitude_m=100.0, dt=0.002)
            cancelled = False

            def progress(item):
                nonlocal cancelled
                if (
                    item.phase is SimulationPhase.WRITING
                    and item.completed >= 256
                ):
                    cancelled = True

            with self.assertRaises(SimulationCancelled):
                run_debris_simulation_request(
                    request,
                    progress_callback=progress,
                    cancellation_check=lambda: cancelled,
                )

            self.assertEqual(output.read_text(encoding="utf-8"), "existing")
            self.assertEqual(list(output.parent.glob(f".{output.name}.*.tmp")), [])

    def test_debris_output_uses_phase_colours_and_google_earth_geometry_settings(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "trajectory.kml"
            run_debris_simulation_request(make_request(output))

            namespace = {"kml": "http://www.opengis.net/kml/2.2"}
            root = ET.parse(output).getroot()
            styles = {
                style.get("id"): style
                for style in root.findall("kml:Document/kml:Style", namespace)
            }
            airborne_style = styles["airborneTrackLine"]
            self.assertEqual(
                airborne_style.find("kml:LineStyle/kml:color", namespace).text,
                "aaff0000",
            )
            self.assertEqual(
                airborne_style.find("kml:LineStyle/kml:width", namespace).text,
                "6",
            )
            self.assertEqual(
                airborne_style.find("kml:PolyStyle/kml:color", namespace).text,
                "33ff0000",
            )
            self.assertEqual(
                styles["groundRunLine"].find("kml:LineStyle/kml:color", namespace).text,
                "aa0000ff",
            )
            self.assertEqual(
                styles["debrisZone"].find("kml:LineStyle/kml:color", namespace).text,
                "aa0000ff",
            )
            self.assertEqual(
                styles["debrisZone"].find("kml:PolyStyle/kml:color", namespace).text,
                "7f0000ff",
            )

            paths = {
                placemark.find("kml:name", namespace).text: placemark
                for placemark in root.findall("kml:Document/kml:Placemark", namespace)
            }
            self.assertEqual(
                paths["Airborne"].find("kml:styleUrl", namespace).text,
                "#airborneTrackLine",
            )
            self.assertEqual(
                paths["Ground run"].find("kml:styleUrl", namespace).text,
                "#groundRunLine",
            )
            self.assertEqual(
                paths["Debris zone"].find("kml:styleUrl", namespace).text,
                "#debrisZone",
            )
            airborne = paths["Airborne"].find("kml:LineString", namespace)
            ground_run = paths["Ground run"].find("kml:LineString", namespace)
            self.assertEqual(airborne.find("kml:extrude", namespace).text, "1")
            self.assertEqual(airborne.find("kml:tessellate", namespace).text, "0")
            self.assertEqual(airborne.find("kml:altitudeMode", namespace).text, "absolute")
            self.assertIsNone(ground_run.find("kml:extrude", namespace))
            self.assertEqual(ground_run.find("kml:tessellate", namespace).text, "1")
            self.assertEqual(ground_run.find("kml:altitudeMode", namespace).text, "clampToGround")


class DebrisSimulationWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def run_worker(self, runner):
        token = CancellationToken()
        request = make_request("unused.kml")
        thread = QThread()
        worker = DebrisSimulationWorker(request, token, runner=runner)
        worker.moveToThread(thread)
        succeeded = QSignalSpy(worker.succeeded)
        cancelled = QSignalSpy(worker.cancelled)
        failed = QSignalSpy(worker.failed)
        finished = QSignalSpy(thread.finished)
        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.start()
        return token, thread, worker, succeeded, cancelled, failed, finished

    def wait_for(self, spy, count=1):
        while len(spy) < count:
            self.assertTrue(spy.wait(2000), "Timed out waiting for a Qt signal")

    def test_success_runs_off_the_application_thread_and_emits_once(self):
        worker_thread_ids = []

        def runner(request, **_):
            worker_thread_ids.append(threading.get_ident())
            return make_result(request.output_file)

        _, _, worker, succeeded, cancelled, failed, finished = self.run_worker(runner)
        self.wait_for(finished)

        self.assertNotEqual(worker_thread_ids, [threading.get_ident()])
        self.assertEqual(len(succeeded), 1)
        self.assertEqual(len(cancelled), 0)
        self.assertEqual(len(failed), 0)
        self.assertIsNotNone(worker)

    def test_direct_token_cancels_busy_worker_without_queued_slot(self):
        reached_worker = threading.Event()

        def runner(_, *, cancellation_check, **__):
            reached_worker.set()
            while not cancellation_check():
                pass
            raise SimulationCancelled()

        token, _, worker, succeeded, cancelled, failed, finished = self.run_worker(runner)
        self.assertTrue(reached_worker.wait(2))
        main_event_processed = []
        QTimer.singleShot(0, lambda: main_event_processed.append(True))
        self.app.processEvents()
        self.assertEqual(main_event_processed, [True])
        token.cancel()
        self.wait_for(finished)

        self.assertEqual(len(succeeded), 0)
        self.assertEqual(len(cancelled), 1)
        self.assertEqual(len(failed), 0)
        self.assertIsNotNone(worker)

    def test_exception_is_delivered_with_traceback(self):
        def runner(*_, **__):
            raise RuntimeError("synthetic failure")

        _, _, worker, succeeded, cancelled, failed, finished = self.run_worker(runner)
        self.wait_for(finished)

        self.assertEqual(len(succeeded), 0)
        self.assertEqual(len(cancelled), 0)
        self.assertEqual(len(failed), 1)
        failure = failed[0][0]
        self.assertEqual(failure.exception_type, "RuntimeError")
        self.assertIn("synthetic failure", failure.message)
        self.assertIn("RuntimeError: synthetic failure", failure.traceback)
        self.assertIsNotNone(worker)


class HoldingWorker(DebrisSimulationWorker):
    entered = threading.Event()
    allow_progress = threading.Event()

    @classmethod
    def reset(cls):
        cls.entered = threading.Event()
        cls.allow_progress = threading.Event()

    def __init__(self, request, token):
        def runner(_, *, progress_callback, cancellation_check):
            self.entered.set()
            self.allow_progress.wait(2)
            progress_callback(
                SimulationProgress(
                    SimulationPhase.SIMULATING,
                    1,
                    2,
                    "Synthetic progress",
                )
            )
            while not cancellation_check():
                pass
            raise SimulationCancelled()

        super().__init__(request, token, runner=runner)


class DebrisSimulationUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.app_patch = patch(
            "pages.debris_page.app_data_path",
            side_effect=lambda relative: str(self.root / "app-data" / relative),
        )
        self.resource_patch = patch(
            "pages.debris_page.resource_path",
            side_effect=lambda relative: str(self.root / "bundle" / relative),
        )
        self.app_patch.start()
        self.resource_patch.start()
        HoldingWorker.reset()
        self.page = DebrisPage()
        self.page.worker_class = HoldingWorker

    def tearDown(self):
        if self.page.has_active_simulation():
            self.page.cancel_simulation(silent=True)
            thread = self.page._simulation_thread
            if thread is not None:
                finished = QSignalSpy(thread.finished)
                while len(finished) == 0:
                    finished.wait(2000)
        self.page.close()
        self.resource_patch.stop()
        self.app_patch.stop()
        self.temp_dir.cleanup()

    def test_controls_progress_and_cancellation_follow_state_machine(self):
        busy = QSignalSpy(self.page.simulation_busy_changed)
        self.assertTrue(self.page._start_simulation(make_request("unused.kml")))
        self.assertTrue(HoldingWorker.entered.wait(2))
        worker_progress = QSignalSpy(self.page._simulation_worker.progress)
        HoldingWorker.allow_progress.set()
        while len(worker_progress) == 0:
            self.assertTrue(worker_progress.wait(2000))
        self.app.processEvents()

        self.assertEqual(self.page._simulation_state, SimulationUiState.RUNNING)
        self.assertFalse(self.page.run_btn.isEnabled())
        self.assertFalse(self.page.presets_widget.isEnabled())
        self.assertFalse(self.page.config_widget.isEnabled())
        self.assertTrue(self.page.cancel_simulation_btn.isEnabled())
        self.assertEqual(self.page.simulation_progress_bar.value(), 50)
        self.assertEqual(self.page.simulation_status_label.text(), "Synthetic progress")
        self.assertFalse(self.page._start_simulation(make_request("second.kml")))

        with patch.object(QMessageBox, "information") as information:
            self.assertTrue(self.page.cancel_simulation())
            self.assertEqual(
                self.page._simulation_state, SimulationUiState.CANCELLING
            )
            while len(busy) < 2:
                self.assertTrue(busy.wait(2000))

        self.assertFalse(self.page.has_active_simulation())
        self.assertTrue(self.page.run_btn.isEnabled())
        self.assertTrue(self.page.presets_widget.isEnabled())
        self.assertTrue(self.page.config_widget.isEnabled())
        self.assertFalse(self.page.cancel_simulation_btn.isVisible())
        self.assertIn("not changed", self.page.simulation_status_label.text())
        information.assert_called_once()

    def test_only_success_updates_summary_and_failure_keeps_it_unchanged(self):
        self.page._set_simulation_state(SimulationUiState.RUNNING)
        self.page._on_simulation_succeeded(make_result("output.kml"))
        with patch.object(QMessageBox, "information") as information:
            self.page._on_simulation_thread_finished()

        self.assertEqual(self.page.summary_heading.text(), "Track used (deg): 90.0")
        self.assertEqual(
            self.page.summary_total.text(),
            "Total ground‑planar distance (m): 12.0",
        )
        information.assert_called_once()

        previous_summary = self.page.summary_total.text()
        self.page._set_simulation_state(SimulationUiState.RUNNING)
        self.page._on_simulation_failed(
            SimulationFailure(
                exception_type="RuntimeError",
                message="broken",
                traceback="details",
            )
        )
        with (
            patch.object(QMessageBox, "exec") as execute,
            patch.object(QMessageBox, "setDetailedText") as detailed_text,
        ):
            self.page._on_simulation_thread_finished()

        self.assertEqual(self.page.summary_total.text(), previous_summary)
        self.assertEqual(self.page.simulation_status_label.text(), "Simulation failed.")
        execute.assert_called_once()
        detailed_text.assert_called_once_with("details")


class AppCloseLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt_app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.app_patch = patch(
            "pages.debris_page.app_data_path",
            side_effect=lambda relative: str(root / "app-data" / relative),
        )
        self.resource_patch = patch(
            "pages.debris_page.resource_path",
            side_effect=lambda relative: str(root / "bundle" / relative),
        )
        self.app_patch.start()
        self.resource_patch.start()
        self.window = App()
        self.window.debris_page._set_simulation_state(SimulationUiState.RUNNING)
        self.assertFalse(self.window.rb_transpose.isEnabled())
        self.assertFalse(self.window.rb_debris.isEnabled())

    def tearDown(self):
        self.window._close_pending = False
        self.window.debris_page._set_simulation_state(SimulationUiState.IDLE)
        self.window.close()
        self.resource_patch.stop()
        self.app_patch.stop()
        self.temp_dir.cleanup()

    def test_close_can_keep_running_or_defer_until_cancel_finishes(self):
        keep_event = QCloseEvent()
        with patch.object(
            QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.No,
        ):
            self.window.closeEvent(keep_event)
        self.assertFalse(keep_event.isAccepted())
        self.assertFalse(self.window._close_pending)

        close_event = QCloseEvent()
        with (
            patch.object(
                QMessageBox,
                "question",
                return_value=QMessageBox.StandardButton.Yes,
            ),
            patch.object(
                self.window.debris_page,
                "cancel_simulation",
                return_value=True,
            ) as cancel,
        ):
            self.window.closeEvent(close_event)
        self.assertFalse(close_event.isAccepted())
        self.assertTrue(self.window._close_pending)
        cancel.assert_called_once_with(silent=True)

        repeated_event = QCloseEvent()
        with patch.object(QMessageBox, "question") as question:
            self.window.closeEvent(repeated_event)
        self.assertFalse(repeated_event.isAccepted())
        question.assert_not_called()

        with patch("app_window.QTimer.singleShot") as single_shot:
            self.window._on_debris_simulation_busy_changed(False)
        single_shot.assert_called_once_with(0, self.window.close)


class ResponsivePageLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        fixture_root = str(PROJECT_ROOT / "tests" / "fixtures")
        self.app_data_patches = [
            patch("pages.debris_page.app_data_path", return_value=fixture_root),
            patch("pages.transpose_page.app_data_path", return_value=fixture_root),
            patch("pages.debris_page.resource_path", return_value=fixture_root),
            patch("pages.transpose_page.resource_path", return_value=fixture_root),
        ]
        for path_patch in self.app_data_patches:
            path_patch.start()
        self.window = App()
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        for path_patch in reversed(self.app_data_patches):
            path_patch.stop()

    def _switch_to_debris(self):
        self.window.rb_debris.click()
        self.app.processEvents()

    def _assert_visible_in_scroll(self, scroll, widget):
        widget_rect = QRect(widget.mapTo(scroll.viewport(), QPoint()), widget.size())
        self.assertTrue(scroll.viewport().rect().contains(widget_rect))

    def test_default_minimum_size_and_scrollable_debris_page(self):
        self.assertEqual(self.window.minimumSize().width(), 900)
        self.assertEqual(self.window.minimumSize().height(), 500)
        self.assertEqual(self.window.size().width(), 900)
        self.assertEqual(self.window.size().height(), 500)

        self._switch_to_debris()
        scroll = self.window.page_scrolls[self.window.debris_page]
        self.assertGreater(scroll.verticalScrollBar().maximum(), 0)
        self.assertLessEqual(self.window.debris_page.width(), scroll.viewport().width())

    def test_keyboard_navigation_reveals_offscreen_debris_control(self):
        self._switch_to_debris()
        scroll = self.window.page_scrolls[self.window.debris_page]
        scroll.setFocus()
        self.app.processEvents()

        for _ in range(40):
            QTest.keyClick(scroll, Qt.Key.Key_Tab)
            self.app.processEvents()
            if self.window.debris_page.run_btn.hasFocus():
                break

        self.assertTrue(self.window.debris_page.run_btn.hasFocus())
        self.assertGreater(scroll.verticalScrollBar().value(), 0)
        self._assert_visible_in_scroll(scroll, self.window.debris_page.run_btn)

        QTest.keyClick(scroll, Qt.Key.Key_Backtab)
        self.app.processEvents()
        self.assertFalse(self.window.debris_page.run_btn.hasFocus())
        self._assert_visible_in_scroll(scroll, self.app.focusWidget())

    def test_both_pages_survive_mode_switching_and_desktop_resizes(self):
        transpose_scroll = self.window.page_scrolls[self.window.transpose_page]
        debris_scroll = self.window.page_scrolls[self.window.debris_page]

        for width, height in ((900, 500), (1024, 768), (1440, 900)):
            self.window.resize(width, height)
            self.app.processEvents()
            self.assertLessEqual(self.window.transpose_page.width(), transpose_scroll.viewport().width())

            self._switch_to_debris()
            self.assertIs(self.window.page_stack.currentWidget(), debris_scroll)
            self.assertLessEqual(self.window.debris_page.width(), debris_scroll.viewport().width())
            self.window.rb_transpose.click()
            self.app.processEvents()
            self.assertIs(self.window.page_stack.currentWidget(), transpose_scroll)
            self.assertIs(transpose_scroll.widget(), self.window.transpose_page)
            self.assertIs(debris_scroll.widget(), self.window.debris_page)


if __name__ == "__main__":
    unittest.main()
