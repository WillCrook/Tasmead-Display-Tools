# Map performance

[Recorded orbit results and outstanding acceptance checks](map-performance-results.md)
describe the current candidate's validation status.

The renderer retains its map, camera and exact KML geometry. Geometry and anchor
changes are compared before a single scheduled animation-frame commit; unchanged
paths are not uploaded again. A no-op revision acknowledges immediately only when
the committed map is already steady. Changed scenes still require Maps' steady
event. This is not a continuous animation loop.

Hidden previews defer unsent geometry work and keep the latest scene. Successfully
rendered, hidden pages freeze when Qt recommends it, after outstanding render
acknowledgements finish. They resume before updates or display. Pages are never
discarded to save power. Maps, camera and document state survive hiding.

## Graphics compatibility and comparison

Production defaults remain unchanged: macOS uses OpenGL unless `QSG_RHI_BACKEND`
is explicitly supplied; Windows uses Qt's default backend. The existing 33 ms
presentation workaround is enabled. Set `TASMEAD_MAP_PRESENTATION_WATCHDOG=0`
to disable its periodic repaint timer for an experiment. Other values preserve
the compatibility behaviour. One-shot refreshes on show and render completion
remain. Graphics backend selection requires restarting the process.

The macOS candidate is available with one opt-in setting:

```sh
TASMEAD_MAP_RENDERING_PROFILE=metal .venv/bin/python src/main.py
```

This selects Metal and disables periodic repainting, without changing Maps or KML
quality. It is a candidate, not the production default. Unset the profile variable
to return to compatibility mode. `QSG_RHI_BACKEND` and
`TASMEAD_MAP_PRESENTATION_WATCHDOG` retain precedence: selecting OpenGL explicitly
also retains its watchdog unless explicitly disabled. Windows/Linux ignore the
Metal profile and keep their existing policy. An unknown profile uses compatibility.
No extra diagnostics or animation loops run in the application.

`Skia Graphite backend = "" not found - falling back to Ganesh!` is a Chromium
renderer-selection message, not proof of software rendering. Ganesh can still use
the GPU. Compare the actual Qt graphics log and Chromium feature status; selecting
Qt Metal does not force Chromium to use Graphite. Do not add Graphite flags simply
to suppress this message.

Do not interpret a 33 ms update timer as a proven 30 fps cap. Likewise, a 60 Hz
JavaScript callback rate does not prove 60 frames were displayed by the Qt window.
Do not change production defaults until frame presentation and resource use have
been checked on supported hardware, including the original freeze scenarios.

## Running the opt-in harness

Run on a visible native desktop, using the project's Python environment. Configure
a Maps key in the application, or provide `TASMEAD_GOOGLE_MAPS_API_KEY` in the
environment. No key is accepted as a command-line argument or included in reports.
The harness loads real Google Maps and uses the configured project's quota.

```sh
.venv/bin/python -m pip install -r requirements-performance.txt
.venv/bin/python tools/map_performance.py --matrix --output /tmp/maps-comparison
```

On Windows use `.venv\Scripts\python.exe` and a suitable new output directory.
Each matrix entry is a fresh process. macOS compares OpenGL/Metal and watchdog
on/off; Windows compares its default backend with watchdog on/off. Each pair runs
bare, representative and dense scenes, in normal and fullscreen windows.

Defaults: one 30-second warmup route, three 30-second movement samples, a 30-second
settled-visible sample, and a 30-second hidden sample. A ten-second settling delay
separates stages. The complete matrix takes over an hour on macOS. Keep the window
unobscured during movement and avoid other GPU/CPU workloads. Match display size,
refresh rate, power mode and device-pixel ratio between comparisons.

The shorter acceptance comparison uses only the compatibility and candidate
configurations (12 launches on Mac), with the requested right-button gesture:

```sh
.venv/bin/python tools/map_performance.py --comparison --motion right-drag \
  --output /tmp/maps-drag-comparison
```

On Mac this compares OpenGL/watchdog-on with Metal/watchdog-off; on Windows it
compares Qt's default backend with the watchdog on/off. Scenes and viewports are
paired consecutively. The harness never changes application defaults based on a
report. Matrix child logs retain sanitised native Chromium startup diagnostics.

For a single configuration:

```sh
.venv/bin/python tools/map_performance.py --backend metal --watchdog off \
  --motion orbit --scene representative --output /tmp/maps-metal
```

The representative fixture has 2,000 vertices and the dense fixture 100,000,
following the same synthetic path near Farnborough. `--dense-points` changes the
stress size. `--kml PATH` uses the parser's selected path for the representative
workload; it is not a visual reproduction of every style/placemark in that file.
The bare case detaches the overlay after initialisation. Camera centre, heading,
range and tilt reset before each warmup/sample, followed by settling. Choose:

- `--motion orbit` (default): a 360-degree heading rotation with fixed centre,
  tilt and range. This isolates orbit from pan and zoom.
- `--motion mixed`: the previous combined pan/orbit/zoom workload.
- `--motion right-drag`: QtTest sends right-button input through the native Qt
  window at nominal 125 Hz along a ten-second back-and-forth horizontal path.
  This includes test-driver overhead and is not physical mouse hardware replay.
- `--motion manual-drag`: the title/terminal announces a five-second countdown
  before each warmup/sample. Right-drag continuously within the map until the
  interval ends. `--countdown-seconds` changes the preparation time. Manual paths
  are not automatically identical; match movement and location between runs.

Drag modes only observe the camera; they never rotate it with JavaScript setters.
Every motion sample must cover at least 95% of the requested duration, travel at
least 30 degrees, and rotate for at least half the interval. Drag modes also require
trusted right-button movement events. Hidden pages and ineffective input reject
the sample. These checks validate the workload, not its frame rate.

If automated dragging fails, the comparison stops with a rejected-input report
(single-run exit code 2). Start a fresh run with `--motion manual-drag`; idle frame
callbacks must never be treated as successful orbit measurements. The local Qt
synthetic driver has delivered drag events without moving Maps, so manual input
remains necessary on this machine unless that input path becomes supported.

For a quick harness check use `--seconds 3 --repeats 1 --settle-seconds 1`.
These short checks are **not acceptance measurements**.

Add `--trace` in a separate diagnostic run to save Chromium traces. Trace recording
adds overhead; compare resource use with tracing disabled. The browser-level
DevTools connection is closed before the hidden sample so inspection does not
prevent page freezing. Debugging is bound to loopback and only enabled in this
standalone harness. Startup graphics logs are captured; verbose per-frame Qt
compositor logging is disabled because it distorts measurements.

## Reports and honest limits

Each report records Qt, WebEngine, Chromium and Maps versions; actual Chromium GPU
details and Qt graphics logs; viewport size, refresh rate, pixel ratio; initial
load time; process-tree CPU and memory samples; and hidden/restored lifecycle state.
No baseline result is overwritten: choose a new output directory for each run.
Reports include motion validation and bounded Qt warning/error diagnostics.
Callback exceptions produce a failed report rather than a silent benchmark abort.

CPU percentages use **100% per fully occupied core**, and include the application
and WebEngine child processes. Summed resident memory may double-count shared
pages. Sampling can miss the last CPU time of a child that exits between samples.
The sampler itself contributes a small amount of CPU work.

Animation callback percentiles measure scheduling. `qt_rendering` separately
observes existing QQuickWidget `afterRendering` callbacks without requesting
repaints or grabbing screenshots. Missing Qt observers are marked unavailable;
multiple render streams are not merged. These callbacks can present repeated
content and are not final display scanout. Sampling includes the brief asynchronous
completion tail; the exact browser motion duration is recorded separately.
When tracing is enabled,
`chromium_frames` reports compositor-frame intervals and raw pipeline outcome
counts, separately for each surface/process. These are not proof of native Qt
window presentation; the native FPS and drop-fraction fields remain unverified.
Open `movement-N.trace.json` in a trace viewer to inspect main-thread, compositor,
GPU task and dropped/partial-frame activity.

For final GPU busy time, power and displayed-frame verification, pair these reports
with Instruments/Metal System Trace on macOS or GPUView/PresentMon on Windows.
Chromium GPU task duration is not hardware GPU utilisation. Native counters are
deliberately left null when unavailable, rather than reported as zero.

Acceptance target on a 60 Hz display: approximately 60 presented fps and less than
1% dropped frames in warmed representative movement, without a repeatable resource
increase for the same workload. Preserve exact geometry and appearance. Check
initial load, repeated reopen, minimise/restore, fullscreen, page switching, rapid
edits, errors and renderer recovery. A passing unit test or synthetic WebGL smoke
test alone cannot satisfy the frame-rate or power gate.

The Metal profile stays opt-in while native presented-frame/GPU measurements or
the right-drag comparison are missing. Long scripted-orbit runs, short probes and
functional lifecycle tests must not be described as passing that gate.

## Regression checks

```sh
QT_QPA_PLATFORM=offscreen TMPDIR=/tmp .venv/bin/python -m pytest -q
QT_QPA_PLATFORM=cocoa TASMEAD_WEBENGINE_PRESENTATION_SMOKE=1 \
  .venv/bin/python -m pytest -q tests/test_webengine_presentation_smoke.py
```

Use the native Windows platform instead of `cocoa` on Windows. Repeat native smoke
checks in fresh launches with the selected backend. The production JavaScript is
also executed against observable Maps/DOM doubles by `test_map_preview_shell.py`;
that test requires Node.js and otherwise skips. Node is not a runtime dependency.

References: [Qt graphics](https://doc.qt.io/qt-6.10/qtwebengine-features.html#hardware-acceleration),
[Qt page lifecycle](https://doc.qt.io/qt-6.10/qwebenginepage.html#lifecycleState-prop),
[Google Maps performance](https://developers.google.com/maps/documentation/javascript/3d/best-practices),
[Chromium frame diagnostics](https://developer.chrome.com/docs/devtools/performance/reference/#frames).
The [Chromium fallback implementation](https://chromium.googlesource.com/chromium/src/+/3eb1bd9e67ab4083dd283cabdff160a2e1e0ffec/gpu/command_buffer/service/service_utils.cc)
explains the Graphite/Ganesh message.
