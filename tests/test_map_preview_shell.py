"""Behavioural JavaScript coverage without Maps access or a graphics driver."""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from test_map_preview_widget import scene_with_two_traces
from map_preview_widget import _PREVIEW_HTML
from services.map_preview import preview_payload


@unittest.skipUnless(shutil.which("node"), "requires Node.js for the shell behaviour harness")
class PreviewShellBehaviourTests(unittest.TestCase):
    def test_production_shell_retains_and_coalesces_exact_geometry(self):
        script = re.findall(r'<script nonce="__NONCE__">(.*?)</script>', _PREVIEW_HTML, re.S)[0]
        result = subprocess.run(
            [shutil.which("node"), str(Path(__file__).with_name("map_preview_shell_harness.cjs"))],
            input=json.dumps({"script": script, "payload": preview_payload(scene_with_two_traces())}),
            text=True, capture_output=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
