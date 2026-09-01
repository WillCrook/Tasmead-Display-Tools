from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from services import (
    ALIGNMENT_PROFILE_FORMAT_VERSION,
    AlignmentMethod,
    AlignmentProfile,
    AlignmentProfileStore,
    AlignmentProfileStoreError,
    PreviewTargetSnapshot,
    TraceAdjustment,
    fingerprint_source_file,
)


class AlignmentProfileStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.source = self.root / "display.kml"
        self.source.write_text("first trace", encoding="utf-8")
        self.store = AlignmentProfileStore(self.root / "profiles")

    def tearDown(self):
        self.tempdir.cleanup()

    def profile(self):
        return AlignmentProfile(
            method=AlignmentMethod.MANUAL,
            runway_source_override={
                "airfieldName": "Original",
                "runway": "24",
                "threshold": "51.0, -1.0",
                "trueHeading": "240",
                "elevationM": "unfinished",
            },
            runway_target={
                "airfieldName": "Target",
                "runway": "09",
                "threshold": "52.0, 0.1",
                "trueHeading": "",
                "elevationM": "",
            },
            manual={
                "targetCoordinate": "draft coordinate",
                "rotationDeg": "not finished",
                "groundElevationM": "71.4",
            },
            preset_selections={
                "sourceRunway": "11111111-1111-1111-1111-111111111111",
                "targetRunway": None,
                "originalTrace": None,
                "targetTrace": "22222222-2222-2222-2222-222222222222",
            },
            preview_signature="alignment-signature",
            preview_adjustment=TraceAdjustment(1.0, 2.0, 3.0, 4.0),
            preview_target_snapshot=PreviewTargetSnapshot(
                method=AlignmentMethod.MANUAL,
                coordinate="52.0, 0.1",
                clockwise_rotation="35",
            ),
        )

    def test_round_trips_raw_drafts_and_preview_adjustment(self):
        fingerprint = fingerprint_source_file(self.source)
        profile = self.profile()

        self.store.save(self.source, fingerprint, profile)
        result = self.store.load(self.source, fingerprint)

        self.assertEqual(result.notice, None)
        self.assertEqual(result.profile, profile)
        document = json.loads(
            self.store.record_path(self.source).read_text(encoding="utf-8")
        )
        self.assertEqual(
            document["formatVersion"], ALIGNMENT_PROFILE_FORMAT_VERSION
        )
        self.assertEqual(document["sourcePath"], str(self.source.resolve()))
        self.assertEqual(
            document["presetSelections"]["targetTrace"],
            "22222222-2222-2222-2222-222222222222",
        )
        self.assertEqual(
            document["preview"]["targetSnapshot"],
            {
                "method": "manual",
                "targetCoordinate": "52.0, 0.1",
                "clockwiseRotation": "35",
            },
        )

    def test_version_one_profile_loads_with_empty_preset_selections(self):
        fingerprint = fingerprint_source_file(self.source)
        self.store.save(self.source, fingerprint, self.profile())
        record = self.store.record_path(self.source)
        document = json.loads(record.read_text(encoding="utf-8"))
        document["formatVersion"] = 1
        document.pop("presetSelections")
        document["preview"].pop("targetSnapshot")
        record.write_text(json.dumps(document), encoding="utf-8")

        result = self.store.load(self.source, fingerprint)

        self.assertIsNotNone(result.profile)
        self.assertEqual(
            result.profile.preset_selections,
            {
                "sourceRunway": None,
                "targetRunway": None,
                "originalTrace": None,
                "targetTrace": None,
            },
        )
        self.assertIsNone(result.profile.preview_target_snapshot)

    def test_version_two_preview_loads_without_restore_snapshot(self):
        fingerprint = fingerprint_source_file(self.source)
        self.store.save(self.source, fingerprint, self.profile())
        record = self.store.record_path(self.source)
        document = json.loads(record.read_text(encoding="utf-8"))
        document["formatVersion"] = 2
        document["preview"].pop("targetSnapshot")
        record.write_text(json.dumps(document), encoding="utf-8")

        result = self.store.load(self.source, fingerprint)

        self.assertIsNotNone(result.profile)
        self.assertEqual(
            result.profile.preview_adjustment,
            TraceAdjustment(1.0, 2.0, 3.0, 4.0),
        )
        self.assertIsNone(result.profile.preview_target_snapshot)

    def test_timestamp_only_change_restores_but_content_change_does_not(self):
        original = fingerprint_source_file(self.source)
        self.store.save(self.source, original, self.profile())

        stat = self.source.stat()
        os.utime(self.source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000))
        touched = fingerprint_source_file(self.source)
        self.assertEqual(touched, original)
        self.assertIsNotNone(self.store.load(self.source, touched).profile)

        self.source.write_text("replacement trace", encoding="utf-8")
        changed = self.store.load(
            self.source,
            fingerprint_source_file(self.source),
        )
        self.assertIsNone(changed.profile)
        self.assertIn("has changed", changed.notice)

    def test_moved_copy_has_independent_path_identity(self):
        fingerprint = fingerprint_source_file(self.source)
        self.store.save(self.source, fingerprint, self.profile())
        moved = self.root / "moved.kml"
        moved.write_bytes(self.source.read_bytes())

        result = self.store.load(moved, fingerprint_source_file(moved))

        self.assertIsNone(result.profile)
        self.assertIsNone(result.notice)
        self.assertNotEqual(
            self.store.record_path(self.source),
            self.store.record_path(moved),
        )

    def test_malformed_record_is_ignored_with_notice(self):
        self.store.directory.mkdir()
        self.store.record_path(self.source).write_text("{bad json", encoding="utf-8")

        result = self.store.load(
            self.source,
            fingerprint_source_file(self.source),
        )

        self.assertIsNone(result.profile)
        self.assertIn("could not be loaded", result.notice)

    def test_write_failure_is_reported_without_partial_record(self):
        blocked = self.root / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")
        store = AlignmentProfileStore(blocked)

        with self.assertRaisesRegex(
            AlignmentProfileStoreError,
            "Could not save alignment settings",
        ):
            store.save(
                self.source,
                fingerprint_source_file(self.source),
                self.profile(),
            )

        self.assertFalse(store.record_path(self.source).exists())


if __name__ == "__main__":
    unittest.main()
