"""Opt-in native Maps benchmark. Run --help; see docs/map-performance.md.

No diagnostics or animation loop is installed in the application itself.
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import time

from map_performance_metrics import (
    ProcessTreeSampler, callback_metrics, chromium_frame_metrics, qt_render_metrics,
    redact_diagnostic, resource_summary, validate_motion,
)
from map_performance_motion import motion_script

ROOT = Path(__file__).resolve().parents[1]


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", action="store_true", help="Separate launches for every backend/watchdog/scene/viewport combination")
    parser.add_argument("--comparison", action="store_true", help="Compare compatibility and candidate across scenes/viewports in separate launches")
    parser.add_argument("--backend", choices=("default", "opengl", "metal"), default="default")
    parser.add_argument("--watchdog", choices=("on", "off"), default="on")
    parser.add_argument("--scene", choices=("bare", "representative", "dense"), default="representative")
    parser.add_argument("--motion", choices=("orbit", "right-drag", "manual-drag", "mixed"), default="orbit")
    parser.add_argument("--countdown-seconds", type=float, default=5, help="Preparation time before each manual drag sample")
    parser.add_argument("--fullscreen", action="store_true")
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--settle-seconds", type=float, default=10)
    parser.add_argument("--dense-points", type=int, default=100_000)
    parser.add_argument("--kml", type=Path, help="Use an actual KML path for the representative workload")
    parser.add_argument("--trace", action="store_true", help="Record a separate Chromium trace per movement sample (adds overhead)")
    parser.add_argument("--output", type=Path, required=True, help="New report directory; existing reports are not overwritten")
    result = parser.parse_args(argv)
    if result.matrix and result.comparison:
        parser.error("choose either --matrix or --comparison")
    if not 0 <= result.countdown_seconds <= 60:
        parser.error("countdown-seconds must be 0..60")
    if not 1 <= result.seconds <= 300 or not 1 <= result.repeats <= 20:
        parser.error("seconds must be 1..300 and repeats 1..20")
    if not 0 <= result.settle_seconds <= 120 or not 2 <= result.dense_points <= 300_000:
        parser.error("settle-seconds must be 0..120 and dense-points 2..300000")
    if result.backend == "metal" and sys.platform != "darwin":
        parser.error("Metal is only supported on macOS")
    if result.output.exists():
        parser.error("output must be a new directory")
    return result


def run_matrix(args):
    args.output.mkdir(parents=True)
    backends = ("opengl", "metal") if sys.platform == "darwin" else ("default",)
    configurations = list(itertools.product(backends, ("on", "off")))
    if args.comparison:
        configurations = [("opengl", "on"), ("metal", "off")] if sys.platform == "darwin" else [("default", "on"), ("default", "off")]
    outcomes = []
    for scene, fullscreen, (backend, watchdog) in itertools.product(
        ("bare", "representative", "dense"), (False, True), configurations
    ):
        name = f"{backend}-{watchdog}-{scene}-{'fullscreen' if fullscreen else 'window'}"
        command = [sys.executable, str(Path(__file__).resolve()),
                   "--backend", backend, "--watchdog", watchdog, "--scene", scene,
                   "--seconds", str(args.seconds), "--repeats", str(args.repeats),
                   "--settle-seconds", str(args.settle_seconds), "--dense-points", str(args.dense_points),
                   "--motion", args.motion, "--countdown-seconds", str(args.countdown_seconds),
                   "--output", str(args.output / name)]
        if fullscreen: command.append("--fullscreen")
        if args.trace: command.append("--trace")
        if args.kml: command.extend(("--kml", str(args.kml)))
        print(name, flush=True)
        # Keep native Chromium stderr (including startup errors) beside the report.
        with (args.output / f"{name}.log").open("w") as log:
            with subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as process:
                for line in process.stdout:
                    log.write(redact_diagnostic(line, os.environ.get("TASMEAD_GOOGLE_MAPS_API_KEY", "")))
                    log.flush()
                exit_code = process.wait()
        outcomes.append({"configuration": name, "exit_code": exit_code})
        (args.output / "matrix.json").write_text(json.dumps(outcomes, indent=2) + "\n")
        if exit_code == 2:
            print("Input validation failed; use --motion manual-drag for a fresh comparison.", flush=True)
            break
    return int(any(item["exit_code"] for item in outcomes))


def main():
    args = arguments()
    if args.matrix or args.comparison:
        return run_matrix(args)
    try:
        import psutil
    except ImportError:
        raise SystemExit("Install optional tooling: python -m pip install -r requirements-performance.txt")
    if os.environ.get("QT_QPA_PLATFORM") in {"offscreen", "minimal"}:
        raise SystemExit("Use a native visible desktop, not QT_QPA_PLATFORM=offscreen/minimal.")
    if args.backend != "default":
        os.environ["QSG_RHI_BACKEND"] = args.backend
    os.environ["TASMEAD_MAP_PRESENTATION_WATCHDOG"] = "1" if args.watchdog == "on" else "0"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    os.environ["QTWEBENGINE_REMOTE_DEBUGGING"] = f"127.0.0.1:{port}"
    # The compositor category logs every texture import and distorts timing.
    os.environ["QT_LOGGING_RULES"] = os.environ.get("QT_LOGGING_RULES", "") + ";qt.webenginecontext=true;qt.webengine.compositor=false"
    sys.path.insert(0, str(ROOT / "src"))
    from webengine_runtime import configure_webengine_runtime
    configure_webengine_runtime()
    from PyQt6.QtCore import QObject, QPoint, QSettings, Qt, QTimer, QUrl, qVersion, PYQT_VERSION_STR, qInstallMessageHandler
    from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkRequest
    from PyQt6.QtWebEngineCore import qWebEngineChromiumVersion, qWebEngineVersion
    from PyQt6.QtWebSockets import QWebSocket
    from PyQt6.QtQuickWidgets import QQuickWidget
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication
    from map_preview_widget import MapPreviewWidget
    from services import KmlCoordinate, KmlDocument, KmlLineString, KmlPlacemark, KmlStyle, PreparedTrace, PreviewScene

    app = QApplication(["tasmead-map-performance"])
    key = os.environ.get("TASMEAD_GOOGLE_MAPS_API_KEY", "").strip() or str(
        QSettings("Tasmead", "Tasmead Display Tools").value("maps/google-maps-api-key", "")
    ).strip()
    if not key:
        raise SystemExit("Configure a Maps key in the application or TASMEAD_GOOGLE_MAPS_API_KEY.")
    args.output.mkdir(parents=True)
    graphics_log = []
    diagnostics = []
    def qt_message(kind, context, message):
        message = redact_diagnostic(message, key)
        if context.category == "qt.webenginecontext" and len(graphics_log) < 100:
            graphics_log.append(message)
        if kind.name in {"QtWarningMsg", "QtCriticalMsg", "QtFatalMsg"}:
            diagnostics.append({"severity": kind.name, "category": context.category, "message": message})
            del diagnostics[:-200]
            print(message, file=sys.stderr, flush=True)
    previous_handler = qInstallMessageHandler(qt_message)

    points_count = args.dense_points if args.scene == "dense" else 2_000
    anchor = KmlCoordinate(-.772, 51.275, 0)
    if args.kml and args.scene == "representative":
        from services.kml_file_handling import parse_kml_track
        track = parse_kml_track(args.kml)
        points = tuple(KmlCoordinate(point.longitude, point.latitude, point.altitude_m or 0) for point in track.points)
        anchor = KmlCoordinate(points[0].longitude, points[0].latitude, 0)
        altitude_mode = track.altitude_mode
        colour = track.source_line_colour or "aaff00ff"
    else:
        points = tuple(KmlCoordinate(
            anchor.longitude + .015 * math.cos(i / (points_count - 1) * math.tau),
            anchor.latitude + .009 * math.sin(i / (points_count - 1) * math.tau),
            200 + 100 * math.sin(i / (points_count - 1) * math.tau * 3)
        ) for i in range(points_count))
        altitude_mode, colour = "relativeToGround", "aaff00ff"
    scene = PreviewScene((PreparedTrace("benchmark", "Benchmark", anchor,
        KmlDocument("Benchmark", (KmlStyle("track", colour, 6),), (
            KmlPlacemark("Benchmark", "#track", KmlLineString(points, altitude_mode, extrude_to_ground=True)),
        )), anchor_altitude_mode=altitude_mode),))

    class Benchmark(QObject):
        def __init__(self):
            super().__init__()
            self.widget = MapPreviewWidget()
            self.widget.setWindowTitle("Tasmead map performance — keep this window visible")
            self.widget.resize(1200, 800)
            self.started = False
            self.finished = False
            self.index = 0
            self.pending = {}
            self.events = []
            self.trace_finish = None
            self.samples = []
            self.phase = None
            self.qt_frames = {}
            self.drag_window = None
            self.drag_position = QPoint()
            self.drag_timer = QTimer(self)
            self.drag_timer.setTimerType(Qt.TimerType.PreciseTimer)
            self.drag_timer.setInterval(8)
            self.drag_timer.timeout.connect(self.drag_step)
            self.sampler = ProcessTreeSampler(psutil, os.getpid())
            self.sample_timer = QTimer(self)
            self.sample_timer.setInterval(1000)
            self.sample_timer.timeout.connect(self.sample_resources)
            self.timeout = QTimer(self)
            self.timeout.setSingleShot(True)
            self.timeout.timeout.connect(lambda: self.fail("Benchmark stage timed out"))
            self.socket = QWebSocket(f"http://127.0.0.1:{port}", parent=self)
            self.network = QNetworkAccessManager(self)
            self.socket.textMessageReceived.connect(self.cdp_message)
            self.socket.connected.connect(self.connected)
            self.socket.errorOccurred.connect(lambda _error: self.fail("Local profiler connection failed"))
            self.report = {
                "platform": platform.platform(), "machine": platform.machine(),
                "qt": qVersion(), "pyqt": PYQT_VERSION_STR,
                "webengine": qWebEngineVersion(), "chromium": qWebEngineChromiumVersion(),
                "requested_backend": args.backend, "qsg_environment": os.environ.get("QSG_RHI_BACKEND"),
                "watchdog": args.watchdog, "scene": args.scene,
                "motion": args.motion, "seconds": args.seconds, "repeats": args.repeats,
                "input_driver": "QtTest QWindow right-button drag, nominal 125 Hz" if args.motion == "right-drag" else args.motion,
                "synthetic": not (args.kml and args.scene == "representative"),
                "vertices": 0 if args.scene == "bare" else len(points),
                "fullscreen": args.fullscreen, "trace_enabled": args.trace, "samples": [],
                "qt_graphics_log": graphics_log,
                "diagnostics": diagnostics,
                "presented_fps": None, "dropped_frame_fraction": None,
                "acceptance": "unverified: correlate Chromium traces with native presentation and GPU measurements",
            }
            if args.fullscreen: self.widget.showFullScreen()
            else: self.widget.show()
            self.opened = time.monotonic()
            self.widget._ensure_web_view()
            self.widget._bridge.render_acknowledged.connect(self.ready)
            self.widget._bridge.render_failed.connect(lambda _g, kind, _message: self.fail(f"Map failed: {kind}"))
            self.widget.set_scene(scene, key)
            self.timeout.start(60_000)

        def safe(self, value):
            if isinstance(value, str):
                value = redact_diagnostic(value, key)
                if self.widget._server:
                    value = value.replace(self.widget._server.url.toString(), "http://127.0.0.1/[preview]")
                return value
            if isinstance(value, dict): return {name: self.safe(item) for name, item in value.items()}
            if isinstance(value, list): return [self.safe(item) for item in value]
            return value

        def save(self):
            (args.output / "report.json").write_text(json.dumps(self.safe(self.report), indent=2) + "\n")

        def fail(self, message, exit_code=1):
            if self.finished: return
            self.report["error"] = message
            self.report["acceptance"] = "failed benchmark; do not change defaults"
            self.save()
            print(message, file=sys.stderr, flush=True)
            self.finish(exit_code)

        def finish(self, exit_code=0):
            self.finished = True
            self.timeout.stop()
            self.sample_timer.stop()
            self.stop_drag()
            self.socket.close()
            self.widget.shutdown()
            self.widget.close()
            app.exit(exit_code)

        def js(self, source, callback):
            self.widget._web_view.page().runJavaScript(source, lambda value: None if self.finished else callback(value))

        def ready(self, generation, revision):
            if self.started or generation != self.widget._page_generation or revision != self.widget._revision: return
            self.started = True
            self.report["initial_load_seconds"] = time.monotonic() - self.opened
            self.timeout.stop()
            self.js("({maps: google.maps.version, dpr: devicePixelRatio, width: innerWidth, height: innerHeight})", self.metadata)

        def metadata(self, value):
            self.report["browser"] = value
            screen = self.widget.screen()
            self.refresh = screen.refreshRate()
            self.report["screen"] = {"refresh_hz": self.refresh, "dpr": screen.devicePixelRatio()}
            self.report["screen"]["logical_size"] = [screen.size().width(), screen.size().height()]
            self.report["viewport"] = {"width": self.widget._web_view.width(), "height": self.widget._web_view.height()}
            # Observe Qt's existing renderer. Never call update(), grab(), or
            # requestUpdate() to produce a diagnostic frame.
            children = self.widget._web_view.findChildren(QQuickWidget)
            self.report["qt_render_probe"] = "afterRendering" if children else "unavailable"
            for index, child in enumerate(children):
                child.quickWindow().afterRendering.connect(
                    lambda index=index: self.observe_qt_frame(index), Qt.ConnectionType.DirectConnection
                )
            self.timeout.start(10_000)
            reply = self.network.get(QNetworkRequest(QUrl(f"http://127.0.0.1:{port}/json/version")))
            def discovered():
                try:
                    target = json.loads(bytes(reply.readAll()))
                    self.socket.open(QUrl(target["webSocketDebuggerUrl"]))
                except Exception:
                    self.fail("Could not discover local Chromium profiling endpoint")
                finally:
                    reply.deleteLater()
            reply.finished.connect(discovered)

        def cdp(self, method, params, callback):
            self.index += 1
            self.pending[self.index] = callback
            self.socket.sendTextMessage(json.dumps({"id": self.index, "method": method, "params": params}))

        def cdp_message(self, raw):
            message = json.loads(raw)
            if "id" in message:
                callback = self.pending.pop(message["id"], None)
                if callback: callback(message)
            elif message.get("method") == "Tracing.dataCollected":
                self.events.extend(message["params"]["value"])
            elif message.get("method") == "Tracing.tracingComplete" and self.trace_finish:
                callback, self.trace_finish = self.trace_finish, None
                callback()

        def connected(self):
            self.timeout.stop()
            self.cdp("SystemInfo.getInfo", {}, self.gpu_info)
            self.timeout.start(10_000)

        def gpu_info(self, reply):
            self.timeout.stop()
            self.report["gpu_info"] = reply.get("result", reply.get("error"))
            if "error" in reply:
                diagnostics.append({"severity": "error", "category": "cdp", "message": "SystemInfo.getInfo failed", "detail": reply["error"]})
            self.save()
            remove = "for (const child of [...map.children]) child.remove();" if args.scene == "bare" else ""
            source = "(() => { const map = document.querySelector('gmp-map-3d'); " + remove + """
                window.__benchmark = {map, base: {lat: map.center.lat, lng: map.center.lng, altitude: map.center.altitude || 0},
                  steady: true, intervals: [], done: false};
                map.addEventListener('gmp-steadychange', event => { window.__benchmark.steady = event.isSteady; });
                map.range = 4000; map.tilt = 60; map.heading = 0;
                return true;
            })()"""
            self.js(source, lambda _value: self.wait_for_steady(self.warmup))

        def wait_for_steady(self, callback):
            self.timeout.start(45_000)
            def poll():
                if self.finished: return
                self.js("window.__benchmark.steady", received)
            def received(steady):
                if steady:
                    self.timeout.stop()
                    QTimer.singleShot(round(args.settle_seconds * 1000), callback)
                else: QTimer.singleShot(250, poll)
            poll()

        def observe_qt_frame(self, index):
            if self.phase is not None and not self.finished:
                self.qt_frames.setdefault(index, []).append(time.monotonic_ns())

        def stop_drag(self):
            self.drag_timer.stop()
            if self.drag_window is not None:
                window, self.drag_window = self.drag_window, None
                QTest.mouseRelease(window, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, self.drag_position, 0)

        def drag_step(self):
            elapsed = time.monotonic() - self.drag_started
            # A fixed ten-second back-and-forth path: warmed repeated samples
            # visit the same view rather than progressively uncovering new tiles.
            fraction = .5 + .3 * math.sin(elapsed / 10 * math.tau)
            point = QPoint(round(self.widget._web_view.width() * fraction), self.widget._web_view.height() // 2)
            self.drag_position = self.widget._web_view.mapTo(self.widget, point)
            QTest.mouseMove(self.drag_window, self.drag_position, 0)

        def animate(self, seconds, callback):
            # rAF only observes in drag modes; real input must move Maps itself.
            self.timeout.start(round((seconds + 20) * 1000))
            def started(_value):
                if args.motion == "right-drag":
                    self.widget.activateWindow()
                    self.widget._web_view.setFocus()
                    self.drag_window = self.widget.windowHandle()
                    point = QPoint(self.widget._web_view.width() // 2, self.widget._web_view.height() // 2)
                    self.drag_position = self.widget._web_view.mapTo(self.widget, point)
                    QTest.mouseMove(self.drag_window, self.drag_position, 0)
                    QTest.mousePress(self.drag_window, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, self.drag_position, 0)
                    self.drag_started = time.monotonic()
                    self.drag_timer.start()
                QTimer.singleShot(round(seconds * 1000), poll)
            def received(value):
                if value and value["done"]:
                    self.timeout.stop()
                    self.stop_drag()
                    sample = value["sample"]
                    sample["validation"] = validate_motion(sample, args.motion, seconds)
                    callback(sample)
                else: QTimer.singleShot(250, poll)
            def poll():
                if not self.finished:
                    self.js("({done: window.__benchmark.done, sample: window.__benchmark.done ? window.__benchmark.sample : null})", received)
            self.js(motion_script(args.motion, seconds), started)

        def prepare_movement(self, callback):
            if args.motion in {"right-drag", "manual-drag"}:
                self.widget.raise_()
                self.widget.activateWindow()
                self.widget._web_view.setFocus()
            def ready(_value):
                self.wait_for_steady(countdown)
            def countdown():
                if args.motion == "manual-drag":
                    message = f"Right-drag continuously for {args.seconds:g}s after {args.countdown_seconds:g}s countdown"
                    self.widget.setWindowTitle(message)
                    print(message, flush=True)
                    QTimer.singleShot(round(args.countdown_seconds * 1000), callback)
                else:
                    callback()
            self.js("(() => { const b=window.__benchmark; b.map.center=b.base; b.map.range=4000; b.map.tilt=60; b.map.heading=0; return true; })()", ready)

        def warmup(self):
            def warmed(sample):
                self.report["warmup_motion"] = {k: v for k, v in sample.items() if k != "intervals"}
                self.save()
                if not sample["validation"]["valid"]:
                    self.fail("Warmup did not produce a valid orbit: " + "; ".join(sample["validation"]["reasons"]) + ". Try --motion manual-drag.", 2)
                else:
                    self.prepare_movement(lambda: self.movement(1))
            self.prepare_movement(lambda: self.animate(args.seconds, warmed))

        def sample_resources(self):
            self.samples.append(self.sampler.sample())

        def begin_resources(self, phase):
            self.phase = phase
            self.samples = []
            self.qt_frames = {}
            self.sampler.sample()
            self.sample_timer.start()
            print(f"{args.backend}/{args.watchdog}/{args.scene}: {phase}", flush=True)
            self.widget.setWindowTitle(f"{phase} — {args.motion} — {args.seconds:g}s")

        def end_resources(self, motion=None):
            self.sample_resources()
            self.sample_timer.stop()
            record = {"phase": self.phase, "resources": resource_summary(self.samples), "resource_samples": self.samples}
            if motion is not None:
                record["callbacks"] = callback_metrics(motion["intervals"], self.refresh)
                record["motion"] = {k: v for k, v in motion.items() if k != "intervals"}
            record["qt_rendering"] = [dict(qt_render_metrics(times, self.refresh), stream=index)
                                      for index, times in self.qt_frames.items()]
            record["page_lifecycle"] = self.widget._web_view.page().lifecycleState().name
            record["page_recommended_state"] = self.widget._web_view.page().recommendedState().name
            record["page_visible"] = self.widget._web_view.page().isVisible()
            record["presentation_active"] = self.widget._presentation_active
            self.report["samples"].append(record)
            self.phase = None
            self.save()

        def movement(self, repeat):
            def start(reply=None):
                if reply and "error" in reply:
                    self.fail("Chromium trace recording is unavailable")
                    return
                self.begin_resources(f"movement-{repeat}")
                self.animate(args.seconds, lambda values: stop(values))
            def stop(sample):
                self.end_resources(sample)
                if not sample["validation"]["valid"]:
                    self.fail("Movement sample rejected: " + "; ".join(sample["validation"]["reasons"]), 2)
                    return
                if args.trace:
                    self.trace_finish = complete_trace
                    self.timeout.start(30_000)
                    self.cdp("Tracing.end", {}, lambda _reply: None)
                else: next_sample()
            def complete_trace():
                self.timeout.stop()
                path = args.output / f"movement-{repeat}.trace.json"
                path.write_text(json.dumps(self.safe({"traceEvents": self.events})))
                self.report["samples"][-1]["chromium_frames"] = chromium_frame_metrics(self.events)
                self.save()
                self.events = []
                next_sample()
            def next_sample():
                callback = (lambda: self.movement(repeat + 1)) if repeat < args.repeats else self.idle
                if repeat < args.repeats:
                    self.prepare_movement(callback)
                else:
                    self.wait_for_steady(callback)
            if args.trace:
                self.events = []
                self.cdp("Tracing.start", {
                    "categories": "-*,benchmark,cc,viz,gpu,devtools.timeline,disabled-by-default-devtools.timeline.frame",
                    "transferMode": "ReportEvents"
                }, start)
                self.timeout.start(10_000)
            else: start()

        def idle(self):
            self.begin_resources("settled-visible")
            QTimer.singleShot(round(args.seconds * 1000), self.hidden)

        def hidden(self):
            self.end_resources()
            # A DevTools connection can prevent freezing. Disconnect for the
            # resource measurement and allow Qt's recommendation to update.
            self.socket.close()
            self.widget.hide()
            QTimer.singleShot(round(args.settle_seconds * 1000), self.measure_hidden)

        def measure_hidden(self):
            self.begin_resources("hidden")
            QTimer.singleShot(round(args.seconds * 1000), self.restore)

        def restore(self):
            self.end_resources()
            if args.fullscreen: self.widget.showFullScreen()
            else: self.widget.show()
            QTimer.singleShot(1000, self.restored)

        def restored(self):
            self.report["restored_lifecycle"] = self.widget._web_view.page().lifecycleState().name
            self.save()
            self.finish()

    benchmark = Benchmark()
    app.setQuitOnLastWindowClosed(False)
    previous_exception_hook = sys.excepthook
    def exception_hook(kind, value, traceback):
        diagnostics.append({"severity": "error", "category": "python", "message": f"{kind.__name__}: {value}"})
        benchmark.fail("Diagnostic callback failed; see report diagnostics")
    sys.excepthook = exception_hook
    try:
        return app.exec()
    finally:
        sys.excepthook = previous_exception_hook
        qInstallMessageHandler(previous_handler)


if __name__ == "__main__":
    raise SystemExit(main())
