"""Small, platform-independent helpers for the opt-in map benchmark."""

from __future__ import annotations

import math
import re
import time


def percentile(values, fraction):
    ordered = sorted(value for value in values if math.isfinite(value))
    if not ordered:
        return None
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    return ordered[lower] + (ordered[min(lower + 1, len(ordered) - 1)] - ordered[lower]) * (position - lower)


def callback_metrics(intervals, refresh_hz):
    """rAF scheduling diagnostics, deliberately not presented-frame metrics."""
    valid = [float(value) for value in intervals if math.isfinite(value) and value > 0]
    period = 1000 / refresh_hz if refresh_hz > 0 else None
    return {
        "metric": "animation_callback_intervals_not_presented_frames",
        "count": len(valid),
        "p50_ms": percentile(valid, .5),
        "p95_ms": percentile(valid, .95),
        "p99_ms": percentile(valid, .99),
        "over_1_5_refresh_periods": sum(value > 1.5 * period for value in valid) if period else None,
    }


def qt_render_metrics(timestamps_ns, refresh_hz):
    """Passive render callbacks can repeat content; they are not displayed FPS."""
    intervals = [(b - a) / 1e6 for a, b in zip(timestamps_ns, timestamps_ns[1:])]
    result = callback_metrics(intervals, refresh_hz)
    result.update(
        metric="qt_render_callbacks_not_native_window_presentation",
        count=len(timestamps_ns),
        callback_rate_hz=(len(timestamps_ns) - 1) * 1e9 / (timestamps_ns[-1] - timestamps_ns[0])
        if len(timestamps_ns) > 1 and timestamps_ns[-1] > timestamps_ns[0] else None,
        native_presented_fps=None,
    )
    return result


def validate_motion(sample, motion, seconds):
    """Reject idle, hidden, short and ineffective input samples explicitly."""
    problems = []
    duration = sample.get("duration_ms", 0)
    travel = sample.get("heading_travel_degrees", 0)
    moving = sample.get("moving_ms", 0)
    if not math.isfinite(duration) or duration < seconds * 1000 * .95:
        problems.append("sample was shorter than the requested duration")
    if not math.isfinite(travel) or travel < 30:
        problems.append("camera heading travelled less than 30 degrees")
    if not math.isfinite(moving) or moving < duration * .5:
        problems.append("camera was not rotating for at least half the sample")
    if sample.get("hidden", True):
        problems.append("page was hidden during the sample")
    if motion in {"right-drag", "manual-drag"} and sample.get("right_drag_moves", 0) < 5:
        problems.append("right-button drag events were not observed")
    return {"valid": not problems, "reasons": problems}


def redact_diagnostic(value, key):
    value = value.replace(key, "[redacted-key]") if key else value
    value = re.sub(r"([?&]key=)[^\s&\"'<>]+", r"\1[redacted-key]", value)
    value = re.sub(r"AIza[0-9A-Za-z_-]{35}", "[redacted-key]", value)
    # Qt can include generation URLs or request paths in diagnostics.
    return re.sub(r"(https?://(?:127\.0\.0\.1|localhost):\d+)/[^\s\"'<>)]*", r"\1/[preview]", value)


class ProcessTreeSampler:
    """Include renderer/GPU helpers; avoid counting child CPU twice."""

    def __init__(self, psutil, root_pid):
        self.psutil = psutil
        self.root = psutil.Process(root_pid)
        self.previous = {}
        self.last_time = time.monotonic()
        self.sample()

    def sample(self):
        now = time.monotonic()
        elapsed = now - self.last_time
        cpu_delta = 0.0
        rss = 0
        processes = []
        try:
            processes = [self.root, *self.root.children(recursive=True)]
        except self.psutil.Error:
            pass
        current = {}
        for process in processes:
            try:
                identity = (process.pid, process.create_time())
                times = process.cpu_times()
                cpu = times.user + times.system
                current[identity] = cpu
                # A process born between samples contributes all its CPU time.
                previous = self.previous.get(identity, cpu if not self.previous else 0.0)
                cpu_delta += max(0.0, cpu - previous)
                rss += process.memory_info().rss
            except self.psutil.Error:
                continue
        self.previous = current
        self.last_time = now
        return {
            "monotonic_seconds": now,
            "elapsed_seconds": elapsed,
            "cpu_seconds": cpu_delta,
            "cpu_percent_one_core": 100 * cpu_delta / elapsed if elapsed > 0 else None,
            "rss_bytes_sum": rss,
            "process_count": len(current),
        }


def resource_summary(samples):
    elapsed = sum(item["elapsed_seconds"] for item in samples)
    cpu = sum(item["cpu_seconds"] for item in samples)
    return {
        "cpu_seconds": cpu,
        "cpu_percent_one_core": 100 * cpu / elapsed if elapsed else None,
        "peak_rss_bytes_sum": max((item["rss_bytes_sum"] for item in samples), default=None),
        "gpu_busy_percent": None,
        "gpu_busy_note": "Requires native GPU profiler; Chromium GPU task time is not hardware utilisation.",
    }


def chromium_frame_metrics(events):
    """Describe Chromium's compositor, not the final native Qt window.

    Keep separate streams separate: combining multiple surfaces can invent FPS.
    Missing trace events stay missing rather than implying zero dropped frames.
    """
    displays = {}
    reporters = {}
    for event in events:
        if event.get("name") == "Display::FrameDisplayed":
            displays.setdefault((event.get("pid"), event.get("tid")), []).append(event["ts"])
        if event.get("name") == "PipelineReporter" and event.get("ph") == "b":
            frame = event.get("args", {}).get("chrome_frame_reporter", {})
            if frame:
                identity = (event.get("pid"), frame.get("layer_tree_host_id"))
                counts = reporters.setdefault(identity, {})
                state = frame.get("state", "unknown")
                counts[state] = counts.get(state, 0) + 1
    streams = []
    for (pid, tid), timestamps in displays.items():
        timestamps.sort()
        intervals = [(b - a) / 1000 for a, b in zip(timestamps, timestamps[1:])]
        streams.append({
            "pid": pid, "tid": tid, "count": len(timestamps),
            "compositor_fps": (len(timestamps) - 1) * 1e6 / (timestamps[-1] - timestamps[0])
            if len(timestamps) > 1 and timestamps[-1] > timestamps[0] else None,
            "p50_ms": percentile(intervals, .5), "p95_ms": percentile(intervals, .95),
            "p99_ms": percentile(intervals, .99),
        })
    return {
        "metric": "chromium_compositor_not_native_window_presentation",
        "display_streams": streams,
        "pipeline_states": [{"pid": pid, "layer_tree_host_id": host, "counts": counts}
                            for (pid, host), counts in reporters.items()],
        "native_presented_fps": None,
    }
