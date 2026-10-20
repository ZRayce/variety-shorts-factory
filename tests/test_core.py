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
from app.main import (build_edit_plan, caption_filter_style, caption_style_preset, classify_permission, generate_candidate_windows, normalize_caption_style,
                      parse_youtube_video_url, render_profile, safe_filename, validate_cut)


class SecurityGateTests(unittest.TestCase):
    def test_edit_plan_is_versioned_and_stores_ordered_segments_and_audio_defaults(self):
        asset = {"duration": 120.0, "width": 1920, "height": 1080, "has_audio": True}
        plan = build_edit_plan("asset-1", asset, 4, {"title": "Interview edit", "profile": "short_vertical",
            "content_preset": "podcast", "segments": [{"start_seconds": 30, "end_seconds": 42},
            {"start_seconds": 10, "end_seconds": 28}], "captions_srt": ""})
        self.assertEqual(plan["schema_version"], 1)
        self.assertEqual([segment["order"] for segment in plan["segments"]], [0, 1])
        self.assertEqual(plan["source"]["rights_version"], 4)
        self.assertEqual(plan["audio"]["mode"], "preserve")
        self.assertFalse(plan["captions"]["word_level_available"])
        self.assertEqual(plan["output_profiles"], ["short_vertical"])

    def test_edit_plan_rejects_invalid_segment_bounds_and_profile_total(self):
        asset = {"duration": 120.0, "width": 1920, "height": 1080, "has_audio": True}
        for segments in ([{"start_seconds": -1, "end_seconds": 10}],
                         [{"start_seconds": 0, "end_seconds": 31}, {"start_seconds": 40, "end_seconds": 71}],
                         [{"start_seconds": 4, "end_seconds": 4}]):
            with self.subTest(segments=segments), self.assertRaises(ValueError):
                build_edit_plan("asset-1", asset, 1, {"segments": segments, "profile": "short_vertical"})

    def test_edit_plan_rejects_duplicate_segment_identifiers_and_invalid_candidate_references(self):
        asset = {"duration": 120.0, "width": 1920, "height": 1080, "has_audio": True}
        repeated = [{"id": "same", "start_seconds": 0, "end_seconds": 3},
                    {"id": "same", "start_seconds": 4, "end_seconds": 7}]
        with self.assertRaises(ValueError):
            build_edit_plan("asset-1", asset, 1, {"segments": repeated})
        with self.assertRaises(ValueError):
            build_edit_plan("asset-1", asset, 1, {"segments": [{"start_seconds": 0, "end_seconds": 3,
                                                                  "candidate_id": "../bad"}]})

    def test_candidate_windows_are_bounded_explainable_and_do_not_score_loudness(self):
        candidates = generate_candidate_windows(120, [30, 80], [(27, 29), (101, 104)], 12, 30, 3, 3,
                                                [{"start_seconds": 29, "end_seconds": 31, "text": "opening"}])
        self.assertEqual(len(candidates), 2)
        self.assertTrue(all(0 <= item["start_seconds"] < item["end_seconds"] <= 120 for item in candidates))
        self.assertTrue(all(12 <= item["duration"] <= 30 for item in candidates))
        self.assertTrue(any("scene change" in reason.lower() for reason in candidates[0]["reasons"]))
        self.assertTrue(any("transcript" in signal for signal in candidates[0]["signals"]))
        self.assertTrue(all("loudness" not in str(item).lower() and "virality" not in str(item).lower()
                            for item in candidates))

    def test_candidate_windows_fall_back_to_reviewable_windows_and_reject_bad_ranges(self):
        candidates = generate_candidate_windows(50, [], [], 10, 20, 2, 2)
        self.assertTrue(candidates)
        self.assertTrue(all("evenly sampled" in " ".join(item["reasons"]).lower() for item in candidates))
        for args in [(0, [], [], 5, 20, 2, 2), (20, [float("nan")], [], 5, 20, 2, 2),
                     (20, [], [], 12, 10, 2, 2)]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                generate_candidate_windows(*args)

    def test_caption_style_is_bounded_and_font_filter_values_cannot_be_injected(self):
        style = normalize_caption_style({"font_name": "Segoe UI", "font_size": 34, "color": "#12aBcD",
            "outline": 3, "shadow": 1, "placement": "top", "safe_margin": 72, "max_line_length": 36,
            "wrap": True})
        self.assertEqual(style["font_name"], "Segoe UI")
        self.assertEqual(style["color"], "#12ABCD")
        self.assertEqual(style["placement"], "top")
        self.assertIn("PrimaryColour=&H00CDAB12", caption_filter_style(style))
        for invalid in ({"font_name": "Arial';drawtext=bad"}, {"font_size": 900},
                        {"color": "red"}, {"placement": "auto"}, {"wrap": "yes"}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                normalize_caption_style(invalid)

    def test_caption_wrapping_preserves_srt_timing_and_unicode(self):
        source = "1\n00:00:00,000 --> 00:00:02,000\nA long line with café and 世界 that needs wrapping."
        wrapped = core.wrap_srt_lines(source, 18)
        self.assertIn("00:00:00,000 --> 00:00:02,000", wrapped)
        self.assertIn("café", wrapped)
        self.assertIn("世界", wrapped)
        self.assertTrue(all(len(line) <= 18 for line in wrapped.splitlines()[2:]))

    def test_vertical_and_widescreen_caption_presets_are_distinct(self):
        shorts_name, shorts = caption_style_preset("short_vertical")
        long_name, long_form = caption_style_preset("long_horizontal")
        self.assertEqual(shorts_name, "shorts")
        self.assertEqual(long_name, "long_form")
        self.assertGreater(shorts["font_size"], long_form["font_size"])
        self.assertLess(shorts["max_line_length"], long_form["max_line_length"])

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
            outputs = [SimpleNamespace(stdout='{"streams":[{"width":640,"height":360,"duration":"12","avg_frame_rate":"30000/1001"}],"format":{"duration":"12"}}'),
                       SimpleNamespace(stdout='{"streams":[{"codec_type":"audio"}]}')]
            with patch.object(core.shutil, "which", return_value="ffprobe.exe"), \
                 patch.object(core.subprocess, "run", side_effect=outputs) as probe:
                result = core.probe_media(media)
        self.assertEqual(result, {"duration": 12.0, "width": 640, "height": 360,
                                  "frame_rate": 30000 / 1001, "has_audio": True})
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
