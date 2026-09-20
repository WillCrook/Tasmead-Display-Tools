# Orbit measurements — 20 September 2026

The Metal candidate remains opt-in. These results do **not** establish 60 presented
fps or pass the right-button-drag/GPU acceptance gate.

## Functional verification

The full regression suite passed: 462 tests and 176 subtests, with 7 tests
skipped. Three native tests passed in each of the compatibility and Metal profiles.
They covered WebGL updates before screenshot capture, real Maps initialisation
and editing, repeated reopening, workspace changes, minimise/restore, fullscreen,
camera/geometry retention, and recovery after terminating the test renderer.
These checks establish functional recovery and Qt rendering, not sustained FPS.

## Warmed scripted orbit

Apple M2, Qt/WebEngine 6.10.2, 60 Hz display, DPR 2. The map viewport was
829 × 734 logical pixels, with the synthetic 2,000-vertex extruded KML. Each
configuration used a fresh process, a 30-second warmup and three 30-second heading
rotations, with three-second settling delays. No Chromium tracing ran during
resource sampling. Geometry, camera centre, range, tilt and resolution matched.

The values below are medians across the three movement samples:

| Configuration | Process-tree CPU (100% = one core) | Qt callback rate | Qt interval p95 | Qt interval p99 |
| --- | ---: | ---: | ---: | ---: |
| OpenGL, watchdog on | 111.6% | 59.77 Hz | 21.94 ms | 33.65 ms |
| Metal, watchdog off | 78.7% | 59.97 Hz | 22.23 ms | 23.39 ms |

CPU was about 29% lower with Metal. The long timing tail improved, while the
95th percentile was similar. Qt callbacks can repeat content and occur before
final display presentation; their rate is not displayed FPS. These sequential
runs support the candidate but do not establish performance on all hardware,
fullscreens, dense KML or physical mouse input. GPU busy time was not measured.

Local raw reports from this run are in
`/tmp/tasmead-orbit-validation-20260920/{opengl-on,metal-off}/report.json`.
Temporary reports may be removed by the operating system; this summary retains
the measurement conditions and limitations.

## Right-button input validation

The QtTest QWindow driver delivered 141 trusted right-button movement events in
a three-second warmup, but camera heading changed by less than 0.000001 degrees.
The new motion gate rejected the run with exit code 2. It did not count idle
animation callbacks as an orbit or continue an invalid comparison matrix.

A subsequent bare-map check with focus established before input also failed
validation: the map was focused throughout, received 150 trusted drag moves
(378 pixels of horizontal movement), and still did not rotate. This is a
limitation of the automated reproduction here, not a valid performance sample.

Use `--motion manual-drag` to profile the user's physical gesture. The complete
three-scene/window/fullscreen drag comparison remains unverified. No native
presented-frame/drop-rate or GPU acceptance result is available, so neither
macOS nor Windows defaults have been promoted.

See [the measurement protocol](map-performance.md) for commands, fallback and
the remaining acceptance checks. The Graphite/Ganesh startup message occurred
while GPU acceleration was available; it is not evidence that this warning
caused the orbit stutter.
