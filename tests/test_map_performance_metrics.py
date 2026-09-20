import importlib.util
from pathlib import Path
import unittest


spec = importlib.util.spec_from_file_location(
    "map_performance_metrics", Path(__file__).resolve().parents[1] / "tools/map_performance_metrics.py"
)
metrics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metrics)


class PerformanceMetricTests(unittest.TestCase):
    def test_qt_callback_rate_is_never_reported_as_displayed_fps(self):
        result = metrics.qt_render_metrics([0, 20_000_000, 40_000_000], 60)
        self.assertEqual(result["callback_rate_hz"], 50)
        self.assertEqual(result["p95_ms"], 20)
        self.assertIsNone(result["native_presented_fps"])
        self.assertIn("not_native_window_presentation", result["metric"])
        self.assertIsNone(metrics.qt_render_metrics([], 60)["callback_rate_hz"])

    def test_noop_short_hidden_and_unobserved_drags_are_rejected(self):
        sample = {"duration_ms": 30_000, "heading_travel_degrees": 360,
                  "moving_ms": 29_000, "hidden": False, "right_drag_moves": 100}
        self.assertTrue(metrics.validate_motion(sample, "right-drag", 30)["valid"])
        for field, value in (("duration_ms", 1000), ("heading_travel_degrees", 0),
                             ("moving_ms", 1000), ("hidden", True), ("right_drag_moves", 0),
                             ("heading_travel_degrees", float("nan"))):
            with self.subTest(field=field, value=value):
                result = metrics.validate_motion(dict(sample, **{field: value}), "right-drag", 30)
                self.assertFalse(result["valid"])
                self.assertTrue(result["reasons"])
        self.assertFalse(metrics.validate_motion({}, "manual-drag", 30)["valid"])

    def test_diagnostics_redact_keys_and_tokenised_loopback_urls(self):
        value = metrics.redact_diagnostic('key=secret http://127.0.0.1:123/abc/preview?generation=4 and http://localhost:456/token', "secret")
        self.assertNotIn("secret", value)
        self.assertNotIn("abc", value)
        self.assertNotIn("token", value)
        self.assertIn("[redacted-key]", value)
        self.assertIn("127.0.0.1:123/[preview]", value)
        self.assertEqual(metrics.redact_diagnostic("https://maps.googleapis.com/maps/api/js?key=PRIVATE&v=quarterly", ""),
                         "https://maps.googleapis.com/maps/api/js?key=[redacted-key]&v=quarterly")

    def test_missing_frames_never_imply_success(self):
        value = metrics.chromium_frame_metrics([])
        self.assertIsNone(value["native_presented_fps"])
        self.assertEqual(value["display_streams"], [])
        self.assertIsNone(metrics.callback_metrics([], 60)["p95_ms"])

    def test_callback_stalls_are_reported_as_callbacks(self):
        value = metrics.callback_metrics([16, 16, 16, 50], 60)
        self.assertEqual(value["over_1_5_refresh_periods"], 1)
        self.assertGreater(value["p95_ms"], 40)
        self.assertIn("not_presented_frames", value["metric"])

    def test_compositor_surfaces_are_not_combined_into_inflated_fps(self):
        events = [{"name": "Display::FrameDisplayed", "pid": pid, "tid": 1, "ts": ts}
                  for pid in (1, 2) for ts in (0, 20_000, 40_000)]
        value = metrics.chromium_frame_metrics(events)
        self.assertEqual([stream["compositor_fps"] for stream in value["display_streams"]], [50, 50])
        self.assertIsNone(value["native_presented_fps"])

    def test_resource_average_is_time_weighted_and_does_not_invent_gpu_usage(self):
        value = metrics.resource_summary([
            {"elapsed_seconds": 1, "cpu_seconds": 1, "rss_bytes_sum": 10},
            {"elapsed_seconds": 3, "cpu_seconds": 0, "rss_bytes_sum": 20},
        ])
        self.assertEqual(value["cpu_percent_one_core"], 25)
        self.assertEqual(value["peak_rss_bytes_sum"], 20)
        self.assertIsNone(value["gpu_busy_percent"])
