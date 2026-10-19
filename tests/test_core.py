"""Regression tests for the local media workflow's security gates."""

import os
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

TEST_DATA = tempfile.TemporaryDirectory(prefix="vsf-tests-", dir=Path(__file__).parent)
os.environ.setdefault("VSF_DATA_DIR", TEST_DATA.name)

from app.main import classify_permission, safe_filename, validate_cut


class SecurityGateTests(unittest.TestCase):
    def test_filename_is_reduced_to_safe_supported_basename(self):
        self.assertEqual(safe_filename("clip file.MP4"), "clip_file.MP4")
        with self.assertRaises(ValueError):
            safe_filename(r"C:\\fake\\clip.MP4")
        with self.assertRaises(ValueError):
            safe_filename("../../payload.exe")

    def test_cut_must_fit_within_source_duration_and_short_limit(self):
        self.assertEqual(validate_cut(2.5, 22.5, 60), (2.5, 22.5))
        for start, end, duration in [(-1, 5, 60), (6, 6, 60), (0, 61, 60), (0, 70, 100), (float("nan"), 5, 30)]:
            with self.subTest(start=start, end=end):
                with self.assertRaises(ValueError):
                    validate_cut(start, end, duration)

    def test_permission_requires_verified_grant_that_has_not_expired(self):
        self.assertTrue(classify_permission("granted", None))
        self.assertFalse(classify_permission("unverified", None))
        self.assertFalse(classify_permission("revoked", None))
        expired = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        self.assertFalse(classify_permission("granted", expired))


if __name__ == "__main__":
    unittest.main()
