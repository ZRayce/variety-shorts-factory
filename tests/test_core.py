"""Regression tests for the local media workflow's security gates."""

import os
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

TEST_DATA = tempfile.TemporaryDirectory(prefix="vsf-tests-", dir=Path(__file__).parent)
os.environ.setdefault("VSF_DATA_DIR", TEST_DATA.name)

from app import main as core
from app.main import classify_permission, parse_youtube_video_url, render_profile, safe_filename, validate_cut


class SecurityGateTests(unittest.TestCase):
    def test_filename_is_reduced_to_safe_supported_basename(self):
        self.assertEqual(safe_filename("clip file.MP4"), "clip_file.MP4")
        self.assertEqual(safe_filename("footage.mkv"), "footage.mkv")
        self.assertEqual(safe_filename("footage.avi"), "footage.avi")
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

    def test_youtube_url_parser_accepts_video_links_without_fetching_media(self):
        for url in (
            "https://www.youtube.com/watch?v=AbCdEf12345",
            "https://youtu.be/AbCdEf12345?t=20",
            "https://youtube.com/shorts/AbCdEf12345",
            "https://m.youtube.com/embed/AbCdEf12345",
        ):
            with self.subTest(url=url):
                self.assertEqual(parse_youtube_video_url(url), "AbCdEf12345")

    def test_youtube_parser_rejects_lookalike_hosts_and_non_video_urls(self):
        for url in (
            "https://youtube.com.evil.example/watch?v=AbCdEf12345",
            "https://youtube.com/watch?v=AbCdEf12345&v=OtherVideo99",
            "https://youtube.com/playlist?list=PL1234567890",
            "javascript:alert(1)",
            "https://user:pass@youtube.com/watch?v=AbCdEf12345",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    parse_youtube_video_url(url)

    def test_render_profiles_have_distinct_canvas_and_duration_limits(self):
        self.assertEqual(render_profile("short_vertical"), {"width": 1080, "height": 1920, "max_seconds": 60})
        self.assertEqual(render_profile("long_horizontal"), {"width": 1920, "height": 1080, "max_seconds": None})
        with self.assertRaises(ValueError):
            render_profile("freeform_filter")

    def test_ffprobe_reads_only_bounded_stream_metadata(self):
        with tempfile.TemporaryDirectory(prefix="vsf-probe-test-", dir=Path(__file__).parent) as temporary:
            media = Path(temporary) / "synthetic.mp4"
            media.write_bytes(b"fixture")
            outputs = [SimpleNamespace(stdout='{"streams":[{"width":640,"height":360,"duration":"12"}],"format":{"duration":"12"}}'),
                       SimpleNamespace(stdout='{"streams":[{"codec_type":"audio"}]}')]
            with patch.object(core.shutil, "which", return_value="ffprobe.exe"), \
                 patch.object(core.subprocess, "run", side_effect=outputs) as probe:
                result = core.probe_media(media)
        self.assertEqual(result, {"duration": 12.0, "width": 640, "height": 360, "has_audio": True})
        self.assertTrue(all("-show_entries" in call.args[0] for call in probe.call_args_list))
        self.assertTrue(all("-show_format" not in call.args[0] for call in probe.call_args_list))

    def test_ffprobe_rejects_nonfinite_or_malformed_media_metadata(self):
        with tempfile.TemporaryDirectory(prefix="vsf-probe-invalid-", dir=Path(__file__).parent) as temporary:
            media = Path(temporary) / "malformed.mp4"
            media.write_bytes(b"fixture")
            cases = ['{"streams":[{"width":640,"height":360,"duration":"NaN"}]}',
                     '{"streams":"unexpected"}']
            for output in cases:
                with self.subTest(output=output), \
                     patch.object(core.shutil, "which", return_value="ffprobe.exe"), \
                     patch.object(core.subprocess, "run", return_value=SimpleNamespace(stdout=output)):
                    self.assertIsNone(core.probe_media(media))


if __name__ == "__main__":
    unittest.main()
