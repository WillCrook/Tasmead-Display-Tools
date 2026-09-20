"""Page lifecycle policy independently of Chromium and the platform compositor."""

import unittest
from unittest.mock import patch

from test_map_preview_widget import scene_with_two_traces
from PyQt6.QtWidgets import QApplication
from map_preview_widget import MapPreviewWidget, QWebEnginePage, _PayloadResult


class Page:
    def __init__(self):
        self.visible = False
        self.loading = False
        self.state = QWebEnginePage.LifecycleState.Active
        self.recommended = QWebEnginePage.LifecycleState.Frozen
        self.transitions = []
        self.scripts = []

    def isVisible(self): return self.visible
    def isLoading(self): return self.loading
    def lifecycleState(self): return self.state
    def recommendedState(self): return self.recommended

    def setLifecycleState(self, state):
        self.transitions.append(state)
        self.state = state

    def runJavaScript(self, source):
        assert self.state == QWebEnginePage.LifecycleState.Active
        self.scripts.append(source)


class View:
    def __init__(self, page): self._page = page
    def page(self): return self._page
    def update(self): pass


class PreviewLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["preview-lifecycle-tests"])

    def setUp(self):
        self.widget = MapPreviewWidget()
        self.page = Page()
        self.widget._web_view = View(self.page)
        self.widget._shell_ready = True
        self.widget._page_has_rendered = True
        self.widget._scene = scene_with_two_traces()
        self.widget._page_generation = 1
        self.widget._revision = 5
        self.widget._acknowledged_revision = 5

    def tearDown(self):
        self.widget._web_view = None
        self.widget._server = None
        self.widget.shutdown()
        self.widget.close()

    def test_hidden_settled_page_freezes_without_discarding(self):
        self.page.recommended = QWebEnginePage.LifecycleState.Discarded
        self.widget._update_page_lifecycle()
        self.widget._update_page_lifecycle()
        self.assertEqual(self.page.transitions, [QWebEnginePage.LifecycleState.Frozen])

    def test_initialisation_loading_inflight_and_moving_pages_stay_active(self):
        for attribute, value in (
            ("_page_has_rendered", False), ("_shell_ready", False),
            ("_browser_pending_revision", 5), ("_presentation_active", True),
        ):
            with self.subTest(attribute=attribute), patch.object(self.widget, attribute, value):
                self.widget._update_page_lifecycle()
                self.assertFalse(self.page.transitions)
        self.page.loading = True
        self.widget._update_page_lifecycle()
        self.assertFalse(self.page.transitions)

    def test_visible_page_and_active_recommendation_are_respected(self):
        self.page.visible = True
        self.widget._update_page_lifecycle()
        self.assertFalse(self.page.transitions)
        self.page.visible = False
        self.page.recommended = QWebEnginePage.LifecycleState.Active
        self.widget._update_page_lifecycle()
        self.assertFalse(self.page.transitions)

    def test_visible_restore_and_javascript_thaw_before_use(self):
        self.widget._update_page_lifecycle()
        self.widget._run_javascript("window.tasmead.fitScene();")
        self.assertEqual(len(self.page.scripts), 1)
        self.widget._update_page_lifecycle()
        self.page.visible = True
        self.widget.show()
        self.assertEqual(self.page.state, QWebEnginePage.LifecycleState.Active)

    def test_hidden_updates_keep_only_latest_scene_and_no_worker_runs(self):
        with patch.object(self.widget._payload_pool, "start") as start:
            self.widget._schedule_render(immediate=True, fit_scene=True)
            latest = scene_with_two_traces()
            self.widget._scene = latest
            self.widget._schedule_render(immediate=True)
            self.assertEqual(self.widget._revision, 7)
            self.assertTrue(self.widget._render_deferred)
            self.assertTrue(self.widget._fit_scene_on_next_render)
            self.assertFalse(self.widget._render_timer.isActive())
            start.assert_not_called()
            self.page.visible = True
            self.widget.show()
            self.app.processEvents()
            self.assertEqual(start.call_count, 1)
            self.assertIs(start.call_args.args[0].scene, latest)

    def test_hide_cancels_unsent_work_and_rejects_queued_completion(self):
        self.widget._payload_chunks = ["partial payload"]
        self.widget._payload_transfer_fit = True
        self.widget._defer_hidden_render()
        self.assertTrue(self.widget._render_deferred)
        self.assertFalse(self.widget._payload_chunks)
        self.assertTrue(self.widget._fit_scene_on_next_render)
        self.widget._payload_prepared(_PayloadResult(5, encoded="{}"))
        self.assertFalse(self.page.scripts)
        self.assertFalse(self.widget._payload_transfer_timer.isActive())

    def test_old_inflight_acknowledgement_allows_freeze_but_not_apply(self):
        self.widget._browser_pending_revision = 5
        self.widget._schedule_render(immediate=True)
        self.assertEqual(self.page.state, QWebEnginePage.LifecycleState.Active)
        self.widget._on_render_acknowledged(1, 5)
        self.assertIsNone(self.widget._browser_pending_revision)
        self.assertEqual(self.page.state, QWebEnginePage.LifecycleState.Frozen)
        self.assertEqual(self.widget._acknowledged_revision, -1)
        self.assertFalse(self.widget.apply_button.isEnabled())
        self.assertTrue(self.widget._render_deferred)

    def test_old_page_visibility_and_recommendations_do_not_touch_new_page(self):
        old_page = Page()
        self.widget._update_page_lifecycle(old_page)
        self.widget._page_visibility_changed(old_page)
        self.assertFalse(self.page.transitions)

    def test_hidden_controls_do_not_wake_the_page(self):
        self.widget._update_page_lifecycle()
        self.widget._sync_tool_mode()
        self.widget._sync_selected_trace(fit_scene=True)
        self.widget._sync_measurement_overlay()
        self.assertFalse(self.page.scripts)
        self.assertTrue(self.widget._controls_deferred)
        self.assertEqual(self.page.state, QWebEnginePage.LifecycleState.Frozen)

    def test_native_presentation_mode_does_not_start_periodic_repaints(self):
        self.widget._presentation_watchdog_enabled = False
        self.page.visible = True
        self.widget.show()
        self.widget._on_presentation_state_changed(1, 5, False)
        self.assertTrue(self.widget._presentation_active)
        self.assertFalse(self.widget._presentation_timer.isActive())
        self.widget.hide()
        self.widget.show()
        self.assertFalse(self.widget._presentation_timer.isActive())
