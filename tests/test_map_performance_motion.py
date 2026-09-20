"""Execute diagnostic motion against a tiny observable map; no Maps quota needed."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from map_performance_motion import motion_script
from map_performance import arguments


@unittest.skipUnless(shutil.which("node"), "requires Node.js")
class MotionScriptTests(unittest.TestCase):
    def exercise(self, mode):
        script = """
        const assert = require('node:assert/strict');
        let scheduled = [], writes = [], heading = 359, handlers = new Map();
        const map = {get heading() {return heading;},
          set heading(value) {heading=value; writes.push('heading');},
          set center(value) {writes.push('center');}, set range(value) {writes.push('range');}};
        global.window = {__benchmark: {map, base: {lat: 0, lng: 0, altitude: 0}}};
        global.document = {hidden: false, hasFocus: () => true,
          addEventListener(type, handler) {handlers.set(type, handler);},
          removeEventListener(type, handler) {assert.equal(handlers.get(type), handler); handlers.delete(type);}};
        global.requestAnimationFrame = callback => scheduled.push(callback);
        """ + motion_script(mode, 1) + """
        for (let now=0; now<=1000; now+=10) {
          if (MODE.includes('drag')) {
            heading = (359 + now*.36) % 360;
            handlers.get('pointermove')({buttons: 2, isTrusted: true});
            handlers.get('pointermove')({buttons: 2, isTrusted: false});
          }
          assert.equal(scheduled.length, 1);
          scheduled.shift()(now);
        }
        assert.equal(scheduled.length, 0);
        assert.equal(handlers.size, 0);
        assert.equal(window.__benchmark.done, true);
        console.log(JSON.stringify({writes, sample: window.__benchmark.sample}));
        """.replace("MODE", json.dumps(mode))
        result = subprocess.run([shutil.which("node"), "-e", script], check=True, capture_output=True, text=True)
        return json.loads(result.stdout)

    def test_orbit_changes_heading_without_pan_or_zoom(self):
        result = self.exercise("orbit")
        self.assertEqual(set(result["writes"]), {"heading"})
        self.assertGreater(result["sample"]["heading_travel_degrees"], 350)

    def test_drag_observes_wraparound_motion_without_setting_camera(self):
        for mode in ("right-drag", "manual-drag"):
            with self.subTest(mode=mode):
                result = self.exercise(mode)
                self.assertEqual(result["writes"], [])
                self.assertAlmostEqual(result["sample"]["heading_travel_degrees"], 360)
                self.assertEqual(result["sample"]["right_drag_moves"], 101)
                self.assertEqual(result["sample"]["moving_ms"], 1000)


class BenchmarkArgumentsTests(unittest.TestCase):
    def test_comparison_propagates_selected_motion(self):
        value = arguments(["--comparison", "--motion", "right-drag", "--output", "/unused-map-comparison"])
        self.assertTrue(value.comparison)
        self.assertEqual(value.motion, "right-drag")
        self.assertEqual((value.seconds, value.repeats), (30, 3))

    def test_conflicting_matrix_modes_are_rejected(self):
        with self.assertRaises(SystemExit):
            arguments(["--matrix", "--comparison", "--output", "/unused-map-comparison"])
