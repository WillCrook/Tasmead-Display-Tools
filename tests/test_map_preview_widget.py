from __future__ import annotations

import os
import re
import sys
import unittest
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
LIVE_GOOGLE_MAPS_API_KEY = os.environ.get("TASMEAD_GOOGLE_MAPS_API_KEY", "").strip()
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from webengine_runtime import configure_webengine_runtime


configure_webengine_runtime()

import map_preview_widget as preview_module

from PyQt6.QtCore import QEventLoop, QTimer
from PyQt6.QtTest import QSignalSpy
from PyQt6.QtWidgets import QApplication

from map_preview_widget import (
    MapPreviewBridge,
    MapPreviewWidget,
    PreviewLoopbackServer,
    WEBENGINE_AVAILABLE,
    _PREVIEW_HTML,
    _content_security_policy,
    _render_preview_html,
    _sanitise_diagnostic_location,
)
from services import (
    KmlCoordinate,
    KmlDocument,
    KmlLineString,
    KmlPlacemark,
    KmlStyle,
    LocalEnuFrame,
    PreparedTrace,
    PreviewScene,
    TraceAdjustment,
    destination_point,
)


def scene_with_two_traces():
    anchor = KmlCoordinate(-1.0, 51.0, 0.0)

    def trace(identity, offset):
        document = KmlDocument(
            name=identity,
            styles=(KmlStyle("track", "aaff00ff", 6.0),),
            placemarks=(
                KmlPlacemark(
                    identity,
                    "#track",
                    KmlLineString(
                        (
                            KmlCoordinate(-1.0 + offset, 51.0, 10.0),
                            KmlCoordinate(-0.999 + offset, 51.001, 20.0),
                        ),
                        "relativeToGround",
                        extrude_to_ground=True,
                    ),
                ),
            ),
        )
        return PreparedTrace(identity, identity.title(), anchor, document)

    return PreviewScene((trace("first", 0.0), trace("second", 0.01)))


def parsed_csp(policy):
    directives = {}
    for segment in policy.split(";"):
        values = segment.strip().split()
        if values:
            directives[values[0]] = values[1:]
    return directives


class _InlineElementNonceParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.elements.append((tag, dict(attrs)))


class PreviewShellPolicyTests(unittest.TestCase):
    def test_policy_and_every_inline_element_use_the_same_nonce(self):
        nonce = "unit-test-nonce-123"
        html = _render_preview_html(nonce)
        policy = _content_security_policy(nonce)
        directives = parsed_csp(policy)
        parser = _InlineElementNonceParser()
        parser.feed(html)

        self.assertNotIn("__NONCE__", html)
        self.assertTrue(parser.elements)
        self.assertTrue(
            all(attributes.get("nonce") == nonce for _, attributes in parser.elements)
        )
        self.assertEqual(directives["script-src"].count(f"'nonce-{nonce}'"), 1)
        self.assertEqual(directives["style-src"].count(f"'nonce-{nonce}'"), 1)
        self.assertEqual(directives["style-src-elem"].count(f"'nonce-{nonce}'"), 1)
        self.assertIn(f"script.nonce = '{nonce}'", html)
        self.assertIn('src="qrc:///qtwebchannel/qwebchannel.js"', html)

    def test_only_style_attributes_are_permitted_inline(self):
        directives = parsed_csp(_content_security_policy("unit-test-nonce-123"))

        self.assertEqual(directives["style-src-attr"], ["'unsafe-inline'"])
        self.assertEqual(directives["script-src-attr"], ["'none'"])
        self.assertNotIn("'unsafe-inline'", directives["script-src"])
        self.assertNotIn("'unsafe-inline'", directives["style-src"])
        self.assertNotIn("'unsafe-inline'", directives["style-src-elem"])

    def test_nonce_cannot_inject_a_policy_directive(self):
        for nonce in ("short", "valid-looking; img-src *", "quoted'nonce-value"):
            with self.subTest(nonce=nonce):
                with self.assertRaises(ValueError):
                    _content_security_policy(nonce)

    def test_diagnostic_locations_remove_credentials_paths_queries_and_fragments(self):
        value = "https://user:pass@maps.googleapis.com/maps/api/js?key=SECRET#fragment"
        self.assertEqual(
            _sanitise_diagnostic_location(value),
            "https://maps.googleapis.com",
        )
        self.assertEqual(_sanitise_diagnostic_location("data:text/plain,GEOMETRY"), "data:")
        self.assertNotIn("SECRET", _sanitise_diagnostic_location(value))

    def test_request_generation_requires_the_exact_path_and_bounded_query(self):
        preview_path = "/secret/preview"
        self.assertEqual(
            preview_module._generation_from_request_target(
                "/secret/preview?generation=1",
                preview_path,
            ),
            1,
        )
        self.assertEqual(
            preview_module._generation_from_request_target(
                "/secret/preview?generation=2147483647",
                preview_path,
            ),
            2_147_483_647,
        )
        for target in (
            "/secret/preview",
            "/secret/preview?generation=0",
            "/secret/preview?generation=2147483648",
            "/secret/preview?generation=1&extra=1",
            "/secret/preview?generation=1#fragment",
            "http://127.0.0.1/secret/preview?generation=1",
            "/wrong/preview?generation=1",
        ):
            with self.subTest(target=target):
                self.assertIsNone(
                    preview_module._generation_from_request_target(
                        target,
                        preview_path,
                    )
                )


class MapPreviewControlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["tasmead-preview-tests"])

    def setUp(self):
        self.widget = MapPreviewWidget()

    def tearDown(self):
        self.widget.shutdown()
        self.widget.close()

    def test_per_trace_controls_recompute_from_base_and_apply_acknowledged_scene(self):
        scene = scene_with_two_traces()
        with patch.object(self.widget, "_ensure_web_view", return_value=False):
            self.widget.set_scene(scene, "test-key")

        self.assertEqual(self.widget.trace_selector.count(), 2)
        self.widget.axis_controls["east_m"].setValue(125.5)
        self.widget._render_timer.stop()
        self.assertEqual(self.widget.scene.traces[0].adjustment.east_m, 125.5)
        self.assertEqual(self.widget.scene.traces[1].adjustment.east_m, 0.0)

        self.widget.trace_selector.setCurrentIndex(1)
        self.widget.axis_controls["yaw_deg"].setValue(12.3)
        self.widget._render_timer.stop()
        self.assertEqual(self.widget.scene.traces[0].adjustment.east_m, 125.5)
        self.assertEqual(self.widget.scene.traces[1].adjustment.yaw_deg, 12.3)

        applied = QSignalSpy(self.widget.scene_applied)
        self.assertFalse(self.widget.apply_button.isEnabled())
        self.widget._failed_generation = None
        self.widget._on_render_acknowledged(
            self.widget._page_generation, self.widget._revision
        )
        self.widget.apply_button.click()
        self.assertEqual(len(applied), 1)
        self.assertIs(applied[0][0], self.widget.scene)

    def test_reset_selected_and_reset_all_do_not_move_other_trace_unintentionally(self):
        with patch.object(self.widget, "_ensure_web_view", return_value=False):
            self.widget.set_scene(scene_with_two_traces(), "test-key")
        self.widget.axis_controls["north_m"].setValue(50.0)
        self.widget._render_timer.stop()
        self.widget.trace_selector.setCurrentIndex(1)
        self.widget.axis_controls["up_m"].setValue(20.0)
        self.widget._render_timer.stop()

        self.widget._reset_selected()
        self.widget._render_timer.stop()
        self.assertEqual(self.widget.scene.traces[0].adjustment.north_m, 50.0)
        self.assertTrue(self.widget.scene.traces[1].adjustment.is_zero)

        self.widget._reset_all()
        self.widget._render_timer.stop()
        self.assertTrue(all(trace.adjustment.is_zero for trace in self.widget.scene.traces))

    def test_shell_requires_a_sized_steady_current_revision_before_apply(self):
        self.assertIn("gmp-map-3d { width: 100%; height: 100%; display: block; }", _PREVIEW_HTML)
        self.assertIn("window.location.search", _PREVIEW_HTML)
        self.assertNotIn("window.location.hash", _PREVIEW_HTML)
        self.assertIn("latestRevision", _PREVIEW_HTML)
        self.assertIn("gmp-steadychange", _PREVIEW_HTML)
        self.assertIn("presentationStateChanged", _PREVIEW_HTML)
        self.assertIn("Number(revision) !== state.latestRevision", _PREVIEW_HTML)
        self.assertIn("element.geodesic = Boolean(geometry.tessellate)", _PREVIEW_HTML)

    def test_shell_retains_map_and_reconciles_overlays_by_stable_identity(self):
        self.assertEqual(_PREVIEW_HTML.count("new Map3DElement("), 1)
        self.assertNotIn("host.replaceChildren()", _PREVIEW_HTML)
        self.assertIn("renderedTraces: new Map()", _PREVIEW_HTML)
        self.assertIn("const geometryId = String(geometry.id)", _PREVIEW_HTML)
        self.assertIn("element.path = geometry.coordinates", _PREVIEW_HTML)
        self.assertIn("removeElement(item.element)", _PREVIEW_HTML)
        self.assertIn(
            "if (mapCreated || Boolean(fitRequested)) fitScene();",
            _PREVIEW_HTML,
        )

    def test_map_tool_modes_are_accessible_exclusive_and_default_to_navigation(self):
        self.assertTrue(self.widget.tool_mode_group.exclusive())
        self.assertTrue(self.widget.navigate_tool_button.isChecked())
        self.assertEqual(self.widget.tool_mode_control.accessibleName(), "Map tool")

        self.widget.measure_tool_button.setChecked(True)
        self.assertFalse(self.widget.navigate_tool_button.isChecked())
        self.assertTrue(self.widget.measure_tool_button.isChecked())
        self.assertEqual(self.widget._tool_mode, "measure")

        self.widget.move_anchor_tool_button.setChecked(True)
        self.assertFalse(self.widget.measure_tool_button.isChecked())
        self.assertTrue(self.widget.move_anchor_tool_button.isChecked())
        self.assertEqual(self.widget._tool_mode, "move-anchor")

    def test_bridge_emits_only_valid_map_click_coordinates(self):
        bridge = MapPreviewBridge()
        clicks = QSignalSpy(bridge.map_clicked)

        bridge.mapClicked(7, 51.25, -1.5)
        bridge.mapClicked(7, float("nan"), -1.5)
        bridge.mapClicked(7, 91.0, -1.5)
        bridge.mapClicked(7, 51.25, 181.0)

        self.assertEqual(len(clicks), 1)
        self.assertEqual(list(clicks[0]), [7, 51.25, -1.5])

    def test_measurement_collects_wgs84_legs_and_remains_until_cleared(self):
        self.widget._page_generation = 3
        with patch.object(self.widget, "_run_javascript") as javascript:
            self.widget.measure_tool_button.setChecked(True)
            initial_revision = self.widget._revision
            self.widget._on_map_clicked(3, 51.0, -1.0)
            self.widget._on_map_clicked(3, 51.001, -1.0)

            self.assertEqual(len(self.widget._measurement_points), 2)
            self.assertEqual(self.widget._revision, initial_revision)
            self.assertIn("Last leg:", self.widget.measurement_label.text())
            self.assertIn("Total:", self.widget.measurement_label.text())
            self.assertIn(" NM", self.widget.measurement_label.text())
            self.assertIn(" m", self.widget.measurement_label.text())

            self.widget.navigate_tool_button.setChecked(True)
            self.widget._on_map_clicked(3, 51.002, -1.0)
            self.assertEqual(len(self.widget._measurement_points), 2)
            self.assertFalse(self.widget.measurement_label.isHidden())

            self.widget.measure_tool_button.setChecked(True)
            self.widget._on_map_clicked(3, 51.02, -1.0)
            self.assertIn(" km", self.widget.measurement_label.text())
            self.widget.undo_measurement_button.click()
            self.assertEqual(len(self.widget._measurement_points), 2)
            self.widget.clear_measurement_button.click()
            self.assertFalse(self.widget._measurement_points)

        self.assertTrue(
            any("setMeasurement" in call.args[0] for call in javascript.call_args_list)
        )

    def test_widget_ignores_stale_and_invalid_map_clicks(self):
        self.widget._page_generation = 5
        self.widget.measure_tool_button.setChecked(True)

        self.widget._on_map_clicked(4, 51.0, -1.0)
        self.widget._on_map_clicked(5, float("inf"), -1.0)
        self.widget._on_map_clicked(5, 51.0, 200.0)

        self.assertFalse(self.widget._measurement_points)

    def test_move_anchor_updates_selected_trace_controls_and_scene_in_place(self):
        scene = scene_with_two_traces()
        traces = list(scene.traces)
        traces[0] = traces[0].with_adjustment(
            TraceAdjustment(up_m=25.0, yaw_deg=7.5)
        )
        scene = PreviewScene(tuple(traces))
        with patch.object(self.widget, "_ensure_web_view", return_value=False):
            self.widget.set_scene(scene, "test-key")
        self.widget._failed_generation = None
        self.widget._page_generation = 4
        self.widget.move_anchor_tool_button.setChecked(True)
        destination = destination_point(51.0, -1.0, 1_000.0, 90.0)

        with patch.object(self.widget, "_schedule_render") as render:
            self.widget._on_map_clicked(4, *destination)

        moved = self.widget.scene.traces[0]
        position = LocalEnuFrame(51.0, -1.0).to_enu(*destination)
        self.assertAlmostEqual(moved.adjustment.east_m, position.east_m, places=1)
        self.assertAlmostEqual(moved.adjustment.north_m, position.north_m, places=1)
        self.assertEqual(moved.adjustment.up_m, 25.0)
        self.assertEqual(moved.adjustment.yaw_deg, 7.5)
        self.assertTrue(self.widget.scene.traces[1].adjustment.is_zero)
        self.assertEqual(
            self.widget.axis_controls["east_m"].value(),
            moved.adjustment.east_m,
        )
        self.assertEqual(
            self.widget.axis_controls["north_m"].value(),
            moved.adjustment.north_m,
        )
        render.assert_called_once_with(immediate=True)

    def test_out_of_bounds_anchor_click_leaves_scene_unchanged(self):
        scene = scene_with_two_traces()
        with patch.object(self.widget, "_ensure_web_view", return_value=False):
            self.widget.set_scene(scene, "test-key")
        self.widget._failed_generation = None
        self.widget._page_generation = 4
        self.widget.move_anchor_tool_button.setChecked(True)
        destination = destination_point(51.0, -1.0, 150_000.0, 90.0)

        with patch.object(self.widget, "_schedule_render") as render:
            self.widget._on_map_clicked(4, *destination)

        self.assertIs(self.widget.scene, scene)
        self.assertIn("adjustment bounds", self.widget.tool_help_label.text())
        render.assert_not_called()

    def test_authentication_failure_offers_settings_and_disables_apply(self):
        self.widget._on_render_acknowledged(
            self.widget._page_generation, self.widget._revision
        )
        self.assertTrue(self.widget.apply_button.isEnabled())

        self.widget._on_render_failed(
            self.widget._page_generation, "authentication", "Rejected"
        )

        self.assertFalse(self.widget.apply_button.isEnabled())
        self.assertFalse(self.widget.open_settings_button.isHidden())
        self.assertEqual(self.widget.status_label.text(), "Rejected")

        self.widget._on_render_acknowledged(
            self.widget._page_generation,
            self.widget._revision,
        )
        self.assertFalse(self.widget.apply_button.isEnabled())
        self.assertFalse(self.widget._session_reusable)
        self.assertEqual(self.widget.status_label.text(), "Rejected")

    def test_bridge_replaces_untrusted_failure_text_with_a_fixed_message(self):
        bridge = MapPreviewBridge()
        failures = QSignalSpy(bridge.render_failed)
        bridge.renderFailed(
            3,
            "authentication",
            "https://maps.googleapis.com/maps/api/js?key=SECRET",
        )

        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0][0], 3)
        self.assertEqual(failures[0][1], "authentication")
        self.assertNotIn("SECRET", failures[0][2])
        self.assertNotIn("maps.googleapis.com", failures[0][2])

    def test_bridge_bounds_render_started_identifiers_before_emitting(self):
        bridge = MapPreviewBridge()
        started = QSignalSpy(bridge.render_started)

        bridge.renderStarted(-4, 9_000_000_000)

        self.assertEqual(len(started), 1)
        self.assertEqual(list(started[0]), [0, 2_147_483_647])

    def test_loading_screen_tracks_current_preview_lifecycle(self):
        self.widget._page_generation = 4
        self.widget._revision = 8
        self.widget._api_key = "test-key"

        self.widget._show_loading("Preparing preview…")
        self.assertTrue(self.widget._loading_active)
        self.assertFalse(self.widget.loading_screen.isHidden())
        self.assertEqual(self.widget.loading_message.text(), "Preparing preview…")
        self.assertEqual(
            (
                self.widget.loading_progress.minimum(),
                self.widget.loading_progress.maximum(),
            ),
            (0, 0),
        )

        with patch.object(self.widget, "_schedule_render"):
            self.widget._on_shell_ready(4)
        self.assertEqual(
            self.widget.loading_message.text(), "Loading Google Maps 3D…"
        )
        self.widget._on_render_started(4, 8)
        self.assertEqual(self.widget.loading_message.text(), "Rendering preview…")

        self.widget._on_render_acknowledged(4, 8)
        self.assertFalse(self.widget._loading_active)
        self.assertTrue(self.widget.loading_screen.isHidden())

    def test_stale_render_events_do_not_dismiss_or_update_current_loader(self):
        self.widget._page_generation = 5
        self.widget._revision = 10
        self.widget._show_loading("Loading Google Maps 3D…")

        self.widget._on_render_started(4, 10)
        self.widget._on_render_started(5, 9)
        self.widget._on_render_acknowledged(4, 10)
        self.widget._on_render_acknowledged(5, 9)

        self.assertTrue(self.widget._loading_active)
        self.assertEqual(
            self.widget.loading_message.text(), "Loading Google Maps 3D…"
        )

    def test_ordinary_scene_update_does_not_cover_the_existing_map(self):
        self.widget._hide_loading()

        self.widget._schedule_render()

        self.widget._render_timer.stop()
        self.assertFalse(self.widget._loading_active)
        self.assertTrue(self.widget.loading_screen.isHidden())

    def test_error_hides_loader_and_shutdown_restores_idle_state(self):
        self.widget._show_loading("Preparing preview…")
        self.widget._show_error("network", "Network failure")

        self.assertFalse(self.widget._loading_active)
        self.assertTrue(self.widget.loading_screen.isHidden())

        self.widget.shutdown()
        self.assertFalse(self.widget._loading_active)
        self.assertFalse(self.widget.loading_screen.isHidden())
        self.assertTrue(self.widget.loading_progress.isHidden())

    def test_retry_shows_loader_but_reused_scene_keeps_existing_map_visible(self):
        class FakeWebView:
            def update(self):
                return None

        class FakeRetryWebView:
            def __init__(self):
                self.loaded = []

            def load(self, url):
                self.loaded.append(url)

        class FakeServer:
            def url_for_generation(self, generation):
                return preview_module.QUrl(
                    "http://127.0.0.1:12345/token/preview"
                    f"?generation={generation}"
                )

        self.widget._web_view = FakeWebView()
        self.widget._server = object()
        self.widget._shell_ready = True
        self.widget._session_reusable = True
        self.widget._api_key = "same-key"
        self.widget._scene = scene_with_two_traces()
        self.widget._measurement_points = [(51.0, -1.0), (51.001, -1.0)]
        self.widget._hide_loading()
        try:
            with patch.object(self.widget, "_schedule_render") as render:
                self.assertTrue(
                    self.widget.set_scene(scene_with_two_traces(), "same-key")
                )

            self.assertFalse(self.widget._loading_active)
            self.assertTrue(self.widget.loading_screen.isHidden())
            self.assertEqual(len(self.widget._measurement_points), 2)
            render.assert_called_once_with(immediate=True, fit_scene=False)

            different_scene = PreviewScene(
                (scene_with_two_traces().traces[0],)
            )
            with patch.object(self.widget, "_schedule_render") as render:
                self.assertTrue(
                    self.widget.set_scene(different_scene, "same-key")
                )

            self.assertFalse(self.widget._measurement_points)
            render.assert_called_once_with(immediate=True, fit_scene=True)

            self.widget._hide_loading()
            retry_web_view = FakeRetryWebView()
            self.widget._web_view = retry_web_view
            self.widget._server = FakeServer()
            with patch.object(preview_module, "WEBENGINE_AVAILABLE", True):
                self.assertTrue(self.widget._reload_shell())

            self.assertTrue(self.widget._loading_active)
            self.assertEqual(self.widget.loading_message.text(), "Preparing preview…")
            self.assertEqual(len(retry_web_view.loaded), 1)
        finally:
            self.widget._web_view = None
            self.widget._server = None

    def test_bridge_sanitises_policy_violation_before_emitting(self):
        bridge = MapPreviewBridge()
        violations = QSignalSpy(bridge.security_policy_violation)
        bridge.securityPolicyViolation(
            7,
            "ENFORCE",
            "SCRIPT-SRC-ELEM",
            "https://user:pass@maps.googleapis.com/maps/api/js?key=SECRET#part",
            "data:text/plain,GEOMETRY",
            -5,
            12,
            3,
        )

        self.assertEqual(len(violations), 1)
        values = list(violations[0])
        self.assertEqual(
            values,
            [
                7,
                "enforce",
                "script-src-elem",
                "https://maps.googleapis.com",
                "data:",
                0,
                12,
                3,
            ],
        )
        joined = " ".join(str(value) for value in values)
        self.assertNotIn("SECRET", joined)
        self.assertNotIn("GEOMETRY", joined)

    def test_bridge_bounds_presentation_state_before_emitting(self):
        bridge = MapPreviewBridge()
        changes = QSignalSpy(bridge.presentation_state_changed)

        bridge.presentationStateChanged(-4, 9_000_000_000, False)

        self.assertEqual(len(changes), 1)
        self.assertEqual(list(changes[0]), [0, 2_147_483_647, False])

    def test_presentation_watchdog_tracks_only_the_current_render(self):
        class FakeWebView:
            def __init__(self):
                self.update_count = 0

            def update(self):
                self.update_count += 1

        fake_web_view = FakeWebView()
        self.widget._web_view = fake_web_view
        self.widget._page_generation = 4
        self.widget._revision = 8
        self.widget.show()
        self.app.processEvents()
        try:
            self.widget._on_presentation_state_changed(4, 8, False)
            self.assertTrue(self.widget._presentation_active)
            self.assertTrue(self.widget._presentation_timer.isActive())

            self.widget._on_presentation_state_changed(3, 8, True)
            self.widget._on_presentation_state_changed(4, 7, True)
            self.assertTrue(self.widget._presentation_timer.isActive())

            before_final_update = fake_web_view.update_count
            self.widget._on_presentation_state_changed(4, 8, True)
            self.assertFalse(self.widget._presentation_active)
            self.assertFalse(self.widget._presentation_timer.isActive())
            self.app.processEvents()
            self.assertGreater(fake_web_view.update_count, before_final_update)
        finally:
            self.widget._presentation_timer.stop()
            self.widget._web_view = None

    def test_presentation_watchdog_stops_while_hidden_and_resumes_if_moving(self):
        class FakeWebView:
            def update(self):
                return None

        self.widget._web_view = FakeWebView()
        self.widget._page_generation = 2
        self.widget._revision = 6
        self.widget.show()
        self.app.processEvents()
        try:
            self.widget._on_presentation_state_changed(2, 6, False)
            self.assertTrue(self.widget._presentation_timer.isActive())

            self.widget.hide()
            self.assertFalse(self.widget._presentation_timer.isActive())
            self.assertTrue(self.widget._presentation_active)

            self.widget.show()
            self.assertTrue(self.widget._presentation_timer.isActive())

            self.widget._show_error("render", "Rendering failed")
            self.assertFalse(self.widget._presentation_timer.isActive())
            self.assertFalse(self.widget._presentation_active)
        finally:
            self.widget._presentation_timer.stop()
            self.widget._web_view = None

    def test_reload_and_shutdown_clear_presentation_watchdog(self):
        self.widget._presentation_active = True
        self.widget._presentation_timer.start()

        self.widget._reload_shell()

        self.assertFalse(self.widget._presentation_timer.isActive())
        self.assertFalse(self.widget._presentation_active)

        self.widget._presentation_active = True
        self.widget._presentation_timer.start()
        self.widget.shutdown()
        self.assertFalse(self.widget._presentation_timer.isActive())
        self.assertFalse(self.widget._presentation_active)

    def test_consecutive_shell_reloads_use_distinct_requested_generations(self):
        class FakeServer:
            def __init__(self):
                self.generations = []

            def url_for_generation(self, generation):
                self.generations.append(generation)
                return preview_module.QUrl(
                    f"http://127.0.0.1:12345/token/preview?generation={generation}"
                )

        class FakeWebView:
            def __init__(self):
                self.urls = []

            def load(self, url):
                self.urls.append(url)

        server = FakeServer()
        web_view = FakeWebView()
        self.widget._server = server
        self.widget._web_view = web_view
        try:
            with patch.object(preview_module, "WEBENGINE_AVAILABLE", True):
                self.assertTrue(self.widget._reload_shell())
                self.assertTrue(self.widget._reload_shell())

            self.assertEqual(server.generations, [1, 2])
            self.assertEqual(
                [url.query() for url in web_view.urls],
                ["generation=1", "generation=2"],
            )
            self.assertTrue(all(not url.fragment() for url in web_view.urls))
        finally:
            self.widget._server = None
            self.widget._web_view = None

    def test_healthy_same_key_session_is_reused_for_a_new_scene(self):
        class FakeWebView:
            def update(self):
                return None

        self.widget._api_key = "same-key"
        self.widget._shell_ready = True
        self.widget._session_reusable = True
        self.widget._web_view = FakeWebView()
        self.widget._server = object()
        try:
            self.widget.show()
            self.app.processEvents()
            self.widget.hide()
            self.app.processEvents()
            with (
                patch.object(self.widget, "_schedule_render") as render,
                patch.object(self.widget, "_reload_shell") as reload_shell,
            ):
                started = self.widget.set_scene(
                    scene_with_two_traces(),
                    "same-key",
                )

            self.assertTrue(started)
            render.assert_called_once_with(immediate=True, fit_scene=True)
            reload_shell.assert_not_called()
        finally:
            self.widget._web_view = None
            self.widget._server = None

    def test_changed_key_or_failed_session_forces_a_hard_reload(self):
        self.widget._api_key = "old-key"
        self.widget._shell_ready = True
        self.widget._session_reusable = True
        self.widget._web_view = object()
        self.widget._server = object()
        try:
            with (
                patch.object(self.widget, "_schedule_render") as render,
                patch.object(
                    self.widget,
                    "_reload_shell",
                    return_value=True,
                ) as reload_shell,
            ):
                self.assertTrue(
                    self.widget.set_scene(scene_with_two_traces(), "new-key")
                )

            reload_shell.assert_called_once_with()
            render.assert_not_called()

            self.widget._session_reusable = True
            self.widget._show_error("network", "Network failure")
            self.assertFalse(self.widget._session_reusable)

            with (
                patch.object(self.widget, "_schedule_render") as render,
                patch.object(
                    self.widget,
                    "_reload_shell",
                    return_value=True,
                ) as reload_shell,
            ):
                self.assertTrue(
                    self.widget.set_scene(scene_with_two_traces(), "new-key")
                )

            reload_shell.assert_called_once_with()
            render.assert_not_called()
        finally:
            self.widget._web_view = None
            self.widget._server = None

    def test_renderer_termination_requires_runtime_recreation(self):
        self.widget._session_reusable = True

        self.widget._on_render_process_terminated()

        self.assertTrue(self.widget._web_runtime_recreation_required)
        self.assertFalse(self.widget._session_reusable)
        self.assertIn("renderer stopped", self.widget.status_label.text())

    def test_retry_recreates_a_terminated_web_runtime_before_loading(self):
        class FakeServer:
            def url_for_generation(self, generation):
                return preview_module.QUrl(
                    f"http://127.0.0.1:12345/token/preview?generation={generation}"
                )

        class FakeWebView:
            def __init__(self):
                self.loaded = []

            def load(self, url):
                self.loaded.append(url)

        web_view = FakeWebView()

        def create_runtime():
            self.widget._server = FakeServer()
            self.widget._web_view = web_view

        self.widget._web_runtime_recreation_required = True
        try:
            with (
                patch.object(preview_module, "WEBENGINE_AVAILABLE", True),
                patch.object(
                    self.widget,
                    "_create_web_view",
                    side_effect=create_runtime,
                ) as create_web_view,
            ):
                self.assertTrue(self.widget._reload_shell())

            create_web_view.assert_called_once_with()
            self.assertFalse(self.widget._web_runtime_recreation_required)
            self.assertEqual(len(web_view.loaded), 1)
            self.assertEqual(web_view.loaded[0].query(), "generation=1")
        finally:
            self.widget._server = None
            self.widget._web_view = None

    def test_enforcing_policy_failure_survives_late_acknowledgement(self):
        self.widget._page_generation = 4
        self.widget._revision = 9
        self.widget._on_render_acknowledged(4, 9)
        self.assertTrue(self.widget.apply_button.isEnabled())

        self.widget._on_security_policy_violation(
            4,
            "enforce",
            "script-src-elem",
            "inline",
            "https://maps.googleapis.com",
            10,
            2,
            1,
        )
        failure_message = self.widget.status_label.text()
        self.widget._on_render_acknowledged(4, 9)

        self.assertFalse(self.widget.apply_button.isEnabled())
        self.assertEqual(self.widget.status_label.text(), failure_message)
        self.assertIn("script-src-elem", failure_message)
        self.assertNotEqual(self.widget._acknowledged_revision, 9)

    def test_policy_diagnostics_are_deduplicated_and_retry_generation_clears_failure(self):
        self.widget._page_generation = 2
        for count in (1, 2, 3):
            self.widget._on_security_policy_violation(
                2,
                "enforce",
                "style-src-elem",
                "inline",
                "https://maps.googleapis.com",
                20,
                4,
                count,
            )

        self.assertEqual(len(self.widget._csp_diagnostics), 1)
        self.assertIn("repeated 3 times", self.widget.status_label.text())
        self.assertFalse(self.widget.apply_button.isEnabled())

        self.assertEqual(self.widget._begin_page_generation(), 3)
        self.assertIsNone(self.widget._csp_failed_generation)
        self.assertFalse(self.widget._csp_diagnostics)
        self.widget._revision = 10
        self.widget._on_render_acknowledged(3, 10)
        self.assertTrue(self.widget.apply_button.isEnabled())

    def test_report_only_and_stale_policy_diagnostics_do_not_block_apply(self):
        self.widget._page_generation = 5
        self.widget._revision = 11
        self.widget._on_security_policy_violation(
            4, "enforce", "script-src", "inline", "qrc:", 1, 1, 1
        )
        self.widget._on_security_policy_violation(
            5, "report", "style-src-elem", "inline", "qrc:", 1, 1, 1
        )
        self.widget._on_render_acknowledged(5, 11)

        self.assertTrue(self.widget.apply_button.isEnabled())
        self.assertIsNone(self.widget._csp_failed_generation)

    def test_policy_diagnostic_cardinality_is_bounded(self):
        self.widget._page_generation = 6
        for index in range(100):
            self.widget._on_security_policy_violation(
                6,
                "report",
                "connect-src",
                f"https://blocked-{index}.example",
                "http://127.0.0.1:12345",
                index,
                1,
                1,
            )

        self.assertLessEqual(
            len(self.widget._csp_diagnostics),
            preview_module._MAX_CSP_DIAGNOSTICS,
        )

    def test_shell_ready_timeout_is_a_visible_failure(self):
        self.widget._shell_ready = False
        self.widget._on_shell_ready_timeout()

        self.assertFalse(self.widget.apply_button.isEnabled())
        self.assertIn("did not initialise", self.widget.status_label.text())
        self.assertIn("KML export remains available", self.widget.status_label.text())

    def test_oversized_scene_is_a_visible_failure_and_is_not_simplified(self):
        with patch.object(self.widget, "_ensure_web_view", return_value=False):
            self.widget.set_scene(scene_with_two_traces(), "test-key")
        self.widget._shell_ready = True
        self.widget._failed_generation = None

        with (
            patch.object(preview_module, "_MAX_PAYLOAD_BYTES", 1),
            patch.object(self.widget, "_run_javascript") as javascript,
        ):
            self.widget._render_scene()

        javascript.assert_not_called()
        self.assertIn("too large", self.widget.status_label.text())
        self.assertIn("No vertices were simplified", self.widget.status_label.text())
        self.assertFalse(self.widget.apply_button.isEnabled())

    def test_render_transport_coalesces_fit_requests_and_consumes_them_once(self):
        with patch.object(self.widget, "_ensure_web_view", return_value=False):
            self.widget.set_scene(scene_with_two_traces(), "test-key")
        self.widget._failed_generation = None
        self.widget._shell_ready = True

        with patch.object(self.widget, "_run_javascript") as javascript:
            self.widget._schedule_render(fit_scene=True)
            self.widget._render_timer.stop()
            self.widget._schedule_render()
            self.widget._render_timer.stop()
            self.widget._render_scene()
            first_finish = next(
                call.args[0]
                for call in reversed(javascript.call_args_list)
                if "finishScene" in call.args[0]
            )
            self.assertIn("true", first_finish)

            javascript.reset_mock()
            self.widget._schedule_render()
            self.widget._render_timer.stop()
            self.widget._render_scene()
            second_finish = next(
                call.args[0]
                for call in reversed(javascript.call_args_list)
                if "finishScene" in call.args[0]
            )
            self.assertIn("false", second_finish)


@unittest.skipUnless(
    os.environ.get("TASMEAD_LOOPBACK_SMOKE") == "1",
    "requires opt-in loopback socket access",
)
class PreviewLoopbackTests(unittest.TestCase):
    def test_shell_is_tokenized_non_cacheable_and_contains_no_api_key(self):
        server = PreviewLoopbackServer()
        try:
            with urllib.request.urlopen(
                server.url_for_generation(1).toString(),
                timeout=3,
            ) as response:
                body = response.read().decode("utf-8")
                policy = response.headers["Content-Security-Policy"]
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["Cache-Control"], "no-store, max-age=0")
                self.assertIn("default-src 'none'", policy)
                self.assertIn("'strict-dynamic'", policy)
                self.assertIn("https://*.googleusercontent.com", policy)
                self.assertIn("style-src-attr 'unsafe-inline'", policy)
                self.assertNotIn("test-key", body)
                self.assertIn("window.tasmead", body)
                nonce_match = re.search(r"script-src 'nonce-([^']+)'", policy)
                self.assertIsNotNone(nonce_match)
                parser = _InlineElementNonceParser()
                parser.feed(body)
                self.assertTrue(
                    all(
                        attributes.get("nonce") == nonce_match.group(1)
                        for _, attributes in parser.elements
                    )
                )
                first_nonce = nonce_match.group(1)
            with urllib.request.urlopen(
                server.url_for_generation(2).toString(),
                timeout=3,
            ) as response:
                second_policy = response.headers["Content-Security-Policy"]
                second_nonce = re.search(
                    r"script-src 'nonce-([^']+)'", second_policy
                ).group(1)
            self.assertNotEqual(first_nonce, second_nonce)
            invalid_urls = (
                server.url.toString(),
                server.url.toString() + "?generation=0",
                server.url.toString() + "?generation=2147483648",
                server.url.toString() + "?generation=1&extra=1",
                f"http://127.0.0.1:{server.port}/?generation=1",
            )
            for invalid_url in invalid_urls:
                with self.subTest(invalid_url=invalid_url):
                    with self.assertRaises(urllib.error.HTTPError) as denied:
                        urllib.request.urlopen(invalid_url, timeout=3)
                    self.assertEqual(denied.exception.code, 404)
        finally:
            server.stop()


@unittest.skipUnless(
    WEBENGINE_AVAILABLE and os.environ.get("TASMEAD_WEBENGINE_SMOKE") == "1",
    "requires opt-in Qt WebEngine smoke test",
)
class WebEngineShellSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["tasmead-webengine-smoke"])

    def test_local_shell_reaches_qwebchannel_without_contacting_google(self):
        widget = MapPreviewWidget()
        try:
            self.assertTrue(widget._ensure_web_view())
            ready = QSignalSpy(widget._bridge.shell_ready)
            violations = QSignalSpy(widget._bridge.security_policy_violation)
            widget._api_key = ""
            widget._reload_shell()
            while len(ready) == 0:
                self.assertTrue(ready.wait(5000))
            self.assertIn("No Google Maps API key", widget.status_label.text())

            result = []
            callback_loop = QEventLoop()
            callback_timeout = QTimer()
            callback_timeout.setSingleShot(True)
            callback_timeout.timeout.connect(callback_loop.quit)
            callback_timeout.start(5000)
            widget._web_view.page().runJavaScript(
                "(() => {"
                "const probe = document.createElement('div');"
                "probe.setAttribute('style', 'color: rgb(1, 2, 3)');"
                "document.body.append(probe);"
                "return {margin: getComputedStyle(document.body).margin, "
                "background: getComputedStyle(document.querySelector('#map-host')).backgroundColor, "
                "inlineColor: getComputedStyle(probe).color, "
                "rules: document.styleSheets[0].cssRules.length};"
                "})()",
                lambda value: (result.append(value), callback_loop.quit()),
            )
            if not result:
                callback_loop.exec()
            callback_timeout.stop()

            self.assertTrue(result, "The WebEngine computed-style callback timed out.")
            self.assertEqual(result[0]["margin"], "0px")
            self.assertEqual(result[0]["background"], "rgb(15, 23, 42)")
            self.assertEqual(result[0]["inlineColor"], "rgb(1, 2, 3)")
            self.assertGreater(result[0]["rules"], 0)

            self.assertTrue(widget._reload_shell())
            while len(ready) < 2:
                self.assertTrue(ready.wait(5000))
            self.assertEqual([list(signal)[0] for signal in ready], [1, 2])
            self.assertIn("No Google Maps API key", widget.status_label.text())
            QApplication.processEvents()
            self.assertEqual(len(violations), 0)
        finally:
            widget.shutdown()
            widget.close()
            QApplication.processEvents()

    def test_repeated_browser_csp_violations_cross_the_bridge_once(self):
        widget = MapPreviewWidget()
        try:
            self.assertTrue(widget._ensure_web_view())
            ready = QSignalSpy(widget._bridge.shell_ready)
            violations = QSignalSpy(widget._bridge.security_policy_violation)
            widget._api_key = ""
            widget._reload_shell()
            while len(ready) == 0:
                self.assertTrue(ready.wait(5000))

            widget._web_view.page().runJavaScript(
                "for (let i = 0; i < 100; i += 1) {"
                "fetch('https://example.invalid/csp-test?secret=SENSITIVE').catch(() => {});"
                "}"
            )
            if len(violations) == 0:
                self.assertTrue(violations.wait(5000))
            settle_loop = QEventLoop()
            QTimer.singleShot(200, settle_loop.quit)
            settle_loop.exec()

            self.assertEqual(len(violations), 1)
            self.assertEqual(violations[0][1], "enforce")
            self.assertEqual(violations[0][2], "connect-src")
            self.assertEqual(violations[0][3], "https://example.invalid")
            self.assertNotIn(
                "SENSITIVE",
                " ".join(str(value) for value in violations[0]),
            )
            self.assertFalse(widget.apply_button.isEnabled())
            self.assertIn("security policy", widget.status_label.text())
        finally:
            widget.shutdown()
            widget.close()
            QApplication.processEvents()


@unittest.skipUnless(
    WEBENGINE_AVAILABLE
    and os.environ.get("TASMEAD_LIVE_MAPS_SMOKE") == "1"
    and bool(LIVE_GOOGLE_MAPS_API_KEY),
    "requires opt-in live Google Maps access and an API key",
)
class LiveGoogleMapsCspSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["tasmead-live-maps-smoke"])

    def _run_and_wait(self, widget, action, timeout_ms=30_000):
        outcomes = []
        loop = QEventLoop()
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)

        def acknowledged(generation, revision):
            if (
                generation == widget._page_generation
                and revision == widget._revision
            ):
                outcomes.append(("acknowledged", generation, revision))
                loop.quit()

        def failed(generation, kind, _message):
            if generation == widget._page_generation:
                outcomes.append(("failed", generation, kind))
                loop.quit()

        def policy_violation(generation, disposition, directive, *_details):
            if generation == widget._page_generation and disposition == "enforce":
                outcomes.append(("security-policy", generation, directive))
                loop.quit()

        widget._bridge.render_acknowledged.connect(acknowledged)
        widget._bridge.render_failed.connect(failed)
        widget._bridge.security_policy_violation.connect(policy_violation)
        try:
            timer.start(timeout_ms)
            action()
            if not outcomes:
                loop.exec()
        finally:
            timer.stop()
            widget._bridge.render_acknowledged.disconnect(acknowledged)
            widget._bridge.render_failed.disconnect(failed)
            widget._bridge.security_policy_violation.disconnect(policy_violation)
        return outcomes

    @staticmethod
    def _settle_events(milliseconds=250):
        loop = QEventLoop()
        QTimer.singleShot(milliseconds, loop.quit)
        loop.exec()

    def _evaluate_javascript(self, widget, source, timeout_ms=5_000):
        results = []
        loop = QEventLoop()
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        timer.start(timeout_ms)
        widget._web_view.page().runJavaScript(
            source,
            lambda value: (results.append(value), loop.quit()),
        )
        if not results:
            loop.exec()
        timer.stop()
        self.assertTrue(results, "The live Maps JavaScript probe timed out.")
        return results[0]

    def _map_snapshot(self, widget):
        return self._evaluate_javascript(
            widget,
            "(() => {"
            "const map = document.querySelector('gmp-map-3d');"
            "const line = map && map.querySelector('gmp-polyline-3d');"
            "const path = line && line.path;"
            "const point = path && (Array.isArray(path) ? path[0] : "
            "(typeof path.getAt === 'function' ? path.getAt(0) : null));"
            "const scalar = value => typeof value === 'function' ? value() : value;"
            "return map ? {"
            "retained: Boolean(map.__tasmeadRetainedSmoke),"
            "center: {lat: scalar(map.center.lat), lng: scalar(map.center.lng), "
            "altitude: scalar(map.center.altitude || 0)},"
            "range: Number(map.range), tilt: Number(map.tilt), heading: Number(map.heading),"
            "point: point ? {lat: scalar(point.lat), lng: scalar(point.lng), "
            "altitude: scalar(point.altitude || 0)} : null"
            "} : null;"
            "})()",
        )

    def test_live_map_reaches_steady_state_without_csp_failures(self):
        widget = MapPreviewWidget()
        try:
            self.assertTrue(widget._ensure_web_view())
            violations = QSignalSpy(widget._bridge.security_policy_violation)
            widget.resize(1200, 800)
            widget.show()
            outcomes = self._run_and_wait(
                widget,
                lambda: widget.set_scene(
                    scene_with_two_traces(),
                    LIVE_GOOGLE_MAPS_API_KEY,
                ),
            )

            self.assertTrue(outcomes, "The live Maps preview timed out.")
            self.assertEqual(outcomes[0][0], "acknowledged")
            self._settle_events()
            self.assertTrue(widget.apply_button.isEnabled())
            self.assertEqual(len(violations), 0)

            configured = self._evaluate_javascript(
                widget,
                "(() => {"
                "const map = document.querySelector('gmp-map-3d');"
                "map.__tasmeadRetainedSmoke = true;"
                "map.center = {lat: 51.005, lng: -1.005, altitude: 100};"
                "map.range = 3210; map.tilt = 47; map.heading = 19;"
                "return true;"
                "})()",
            )
            self.assertTrue(configured)
            self._settle_events()
            before_adjustment = self._map_snapshot(widget)
            self.assertIsNotNone(before_adjustment["point"])

            first_generation = widget._page_generation
            widget.hide()
            QApplication.processEvents()
            widget.show()
            outcomes = self._run_and_wait(
                widget,
                lambda: widget.set_scene(
                    scene_with_two_traces(),
                    LIVE_GOOGLE_MAPS_API_KEY,
                ),
            )
            self.assertTrue(outcomes, "The reused Maps preview timed out.")
            self.assertEqual(outcomes[0][0], "acknowledged")
            self.assertEqual(widget._page_generation, first_generation)
            self.assertTrue(widget.apply_button.isEnabled())
            self.assertEqual(len(violations), 0)

            applied = QSignalSpy(widget.scene_applied)
            expected_scene = widget.scene
            widget.apply_button.click()
            self.assertEqual(len(applied), 1)
            self.assertIs(applied[0][0], expected_scene)

            first_revision = widget._revision
            outcomes = self._run_and_wait(
                widget,
                lambda: widget.axis_controls["east_m"].setValue(0.1),
            )
            self.assertTrue(outcomes, "The adjusted live Maps preview timed out.")
            self.assertEqual(outcomes[0][0], "acknowledged")
            self._settle_events()
            self.assertGreater(widget._revision, first_revision)
            self.assertTrue(widget.apply_button.isEnabled())
            self.assertEqual(len(violations), 0)
            after_adjustment = self._map_snapshot(widget)
            self.assertTrue(after_adjustment["retained"])
            for axis in ("lat", "lng", "altitude"):
                self.assertAlmostEqual(
                    after_adjustment["center"][axis],
                    before_adjustment["center"][axis],
                    places=7,
                )
            self.assertAlmostEqual(after_adjustment["range"], 3210.0)
            self.assertAlmostEqual(after_adjustment["tilt"], 47.0)
            self.assertAlmostEqual(after_adjustment["heading"], 19.0)
            self.assertNotEqual(after_adjustment["point"], before_adjustment["point"])
        finally:
            widget.shutdown()
            widget.close()
            QApplication.processEvents()


if __name__ == "__main__":
    unittest.main()
