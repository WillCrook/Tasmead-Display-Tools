"""Opt-in measurements for a private, high-resolution KML editor input."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import time
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from services.kml_editor_operations import (
    KmlEditorOperationCancelled,
    build_crop_preview_scene,
    build_simplification_preview_scene,
    simplify_track,
)
from services.kml_editor_workspace import KmlEditorFileRepository
from services.kml_file_handling import parse_kml_text
from services.map_preview import preview_payload


HIGH_RES_PATH = os.environ.get("TASMEAD_HIGH_RES_KML", "").strip()


@unittest.skipUnless(
    HIGH_RES_PATH,
    "set TASMEAD_HIGH_RES_KML to benchmark a private high-resolution KML",
)
class HighResolutionKmlPerformanceTests(unittest.TestCase):
    def test_private_high_resolution_track(self):
        path = Path(HIGH_RES_PATH).expanduser().resolve(strict=True)
        raw = path.read_bytes()
        loaded = KmlEditorFileRepository().load(path)

        started = time.perf_counter()
        track = parse_kml_text(loaded.contents, source_name=path.name)
        parse_seconds = time.perf_counter() - started
        self.assertGreaterEqual(len(track.points), 2)

        start_index = len(track.points) // 10
        end_index = min(
            len(track.points) - 1,
            max(start_index + 1, len(track.points) * 9 // 10),
        )
        started = time.perf_counter()
        crop_scene = build_crop_preview_scene(
            track,
            start_index,
            end_index,
            trace_id="private-crop",
            label=path.name,
        )
        crop_preview_seconds = time.perf_counter() - started
        crop_payload_bytes = len(
            json.dumps(preview_payload(crop_scene), separators=(",", ":")).encode()
        )

        reductions = {}
        balanced_result = None
        for tolerance in (1.0, 5.0, 20.0):
            started = time.perf_counter()
            result = simplify_track(track, tolerance)
            elapsed = time.perf_counter() - started
            self.assertEqual(result.kept_indices[0], 0)
            self.assertEqual(result.kept_indices[-1], len(track.points) - 1)
            reductions[f"{tolerance:g}m"] = {
                "points": result.result_count,
                "reduction_percent": result.reduction_percent,
                "seconds": elapsed,
            }
            if tolerance == 5.0:
                balanced_result = result

        started = time.perf_counter()
        comparison = build_simplification_preview_scene(
            track,
            balanced_result,
            trace_id="private-reduction",
            label=path.name,
        )
        reduction_preview_seconds = time.perf_counter() - started
        reduction_payload_bytes = len(
            json.dumps(preview_payload(comparison), separators=(",", ":")).encode()
        )

        started = time.perf_counter()
        with self.assertRaises(KmlEditorOperationCancelled):
            simplify_track(track, 5.0, cancellation_check=lambda: True)
        cancellation_seconds = time.perf_counter() - started

        metrics = {
            "path": str(path),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            "points": len(track.points),
            "parse_seconds": parse_seconds,
            "crop_preview_seconds": crop_preview_seconds,
            "crop_payload_bytes": crop_payload_bytes,
            "reductions": reductions,
            "reduction_preview_seconds": reduction_preview_seconds,
            "reduction_payload_bytes": reduction_payload_bytes,
            "cancellation_seconds": cancellation_seconds,
        }
        print("\n" + json.dumps(metrics, indent=2, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
