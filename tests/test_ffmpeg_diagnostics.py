"""Isolated tests for executable detection and temporary FFmpeg verification."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TEST_DATA = tempfile.TemporaryDirectory(prefix="vsf-diagnostics-tests-", dir=Path(__file__).parent)
os.environ.setdefault("VSF_DATA_DIR", TEST_DATA.name)

from app import main as core


class FfmpegDiagnosticsTests(unittest.TestCase):
    def test_configured_executable_wins_and_bare_name_uses_path(self):
        with tempfile.TemporaryDirectory(dir=core.DATA_ROOT) as directory:
            configured = Path(directory) / "configured-ffmpeg.exe"
            configured.write_bytes(b"fixture")
            with patch.object(core.shutil, "which", return_value="path-ffmpeg.exe"):
                self.assertEqual(core.resolve_executable(str(configured)), str(configured.resolve()))
                self.assertEqual(core.resolve_executable("ffmpeg"), str(Path("path-ffmpeg.exe").resolve()))

    def test_known_windows_install_location_is_used_before_path_fallback(self):
        known = Path(r"C:\ffmpeg\bin\ffmpeg.exe")
        original_is_file = Path.is_file

        def is_file(path):
            return True if path == known else original_is_file(path)

        with patch.object(Path, "is_file", is_file), patch.object(core.shutil, "which", return_value=None):
            self.assertEqual(core.resolve_executable("missing-configured-ffmpeg"), str(known.resolve()))

    def test_diagnostics_reports_missing_tools_without_spawning_processes(self):
        with patch.object(core, "resolve_executable", return_value=None), \
             patch.object(core.subprocess, "run") as run:
            result = core.diagnose_media_tools()
        self.assertEqual(result["ffmpeg"]["status"], "Missing")
        self.assertEqual(result["ffprobe"]["status"], "Missing")
        run.assert_not_called()

    def test_diagnostics_reports_version_and_process_failure(self):
        def run(command, **kwargs):
            if command[0] == "ffmpeg.exe":
                return subprocess.CompletedProcess(command, 0, "ffmpeg version 7.1\nbuild info", "")
            return subprocess.CompletedProcess(command, 5, "", "probe failed")

        with patch.object(core, "resolve_executable", side_effect=["ffmpeg.exe", "ffprobe.exe"]), \
             patch.object(core.subprocess, "run", side_effect=run) as mocked_run:
            result = core.diagnose_media_tools()
        self.assertEqual(result["ffmpeg"]["status"], "Available")
        self.assertEqual(result["ffmpeg"]["version"], "ffmpeg version 7.1")
        self.assertEqual(result["ffprobe"]["status"], "Error")
        self.assertIn("code 5", result["ffprobe"]["error"])
        self.assertTrue(all(call.kwargs["timeout"] <= 10 for call in mocked_run.call_args_list))
        self.assertTrue(all(call.kwargs.get("shell") is not True for call in mocked_run.call_args_list))

    def test_diagnostics_handles_permission_and_timeout_errors(self):
        with patch.object(core, "resolve_executable", side_effect=["ffmpeg.exe", "ffprobe.exe"]), \
             patch.object(core.subprocess, "run", side_effect=[PermissionError("access denied"),
                                                                subprocess.TimeoutExpired("ffprobe", 8)]):
            result = core.diagnose_media_tools()
        self.assertEqual(result["ffmpeg"]["status"], "Error")
        self.assertEqual(result["ffprobe"]["status"], "Error")
        self.assertNotIn("environment", json.dumps(result).lower())

    def test_render_test_accepts_verified_synthetic_output_and_cleans_temp_directory(self):
        created_directories = []
        real_temporary_directory = tempfile.TemporaryDirectory

        def temporary_directory(*args, **kwargs):
            result = real_temporary_directory(*args, **kwargs)
            created_directories.append(Path(result.name))
            return result

        def run(command, **kwargs):
            if "-version" in command:
                return subprocess.CompletedProcess(command, 0, f"{Path(command[0]).stem} version 7.1", "")
            if "-f" in command and "lavfi" in command:
                Path(command[-1]).write_bytes(b"synthetic nonempty MP4")
                return subprocess.CompletedProcess(command, 0, "", "")
            return subprocess.CompletedProcess(command, 0,
                json.dumps({"streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
                            "format": {"duration": "2.0"}}), "")

        with patch.object(core, "resolve_executable", side_effect=["ffmpeg.exe", "ffprobe.exe"] * 2), \
             patch.object(core.tempfile, "gettempdir", return_value=str(core.DATA_ROOT)), \
             patch.object(core.tempfile, "TemporaryDirectory", side_effect=temporary_directory), \
             patch.object(core.subprocess, "run", side_effect=run):
            result = core.run_ffmpeg_render_test()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["duration_seconds"], 2.0)
        self.assertEqual(len(created_directories), 1)
        self.assertFalse(created_directories[0].exists())

    def test_render_test_rejects_missing_and_invalid_outputs(self):
        with patch.object(core, "resolve_executable", side_effect=["ffmpeg.exe", "ffprobe.exe"] * 2), \
             patch.object(core.tempfile, "gettempdir", return_value=str(core.DATA_ROOT)), \
             patch.object(core.subprocess, "run", side_effect=lambda command, **kwargs:
                 subprocess.CompletedProcess(command, 0, "version 7.1", "") if "-version" in command else
                 subprocess.CompletedProcess(command, 0, "", "")):
            result = core.run_ffmpeg_render_test()
        self.assertEqual(result["status"], "failed")
        self.assertIn("nonempty", result["detail"])

    def test_render_test_reports_failed_ffmpeg_and_invalid_probe_metadata(self):
        def failed_render(command, **kwargs):
            if "-version" in command:
                return subprocess.CompletedProcess(command, 0, "ffmpeg version 7.1", "")
            return subprocess.CompletedProcess(command, 9, "", "synthetic encoder failure")

        with patch.object(core, "resolve_executable", side_effect=["ffmpeg.exe", "ffprobe.exe"]), \
             patch.object(core.tempfile, "gettempdir", return_value=str(core.DATA_ROOT)), \
             patch.object(core.subprocess, "run", side_effect=failed_render):
            failed = core.run_ffmpeg_render_test()
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["exit_code"], 9)
        self.assertIn("synthetic encoder failure", failed["stderr"])

        def invalid_probe(command, **kwargs):
            if "-version" in command:
                return subprocess.CompletedProcess(command, 0, "version 7.1", "")
            if command[0] == "ffmpeg.exe":
                Path(command[-1]).write_bytes(b"not a valid media file")
                return subprocess.CompletedProcess(command, 0, "", "")
            return subprocess.CompletedProcess(command, 0,
                json.dumps({"streams": [{"codec_type": "audio"}], "format": {"duration": "2.0"}}), "")

        with patch.object(core, "resolve_executable", side_effect=["ffmpeg.exe", "ffprobe.exe"]), \
             patch.object(core.tempfile, "gettempdir", return_value=str(core.DATA_ROOT)), \
             patch.object(core.subprocess, "run", side_effect=invalid_probe):
            invalid = core.run_ffmpeg_render_test()
        self.assertEqual(invalid["status"], "failed")
        self.assertIn("video and audio streams", invalid["detail"])

    def test_command_redaction_hides_credentials(self):
        command = ["ffmpeg.exe", "-headers", "Authorization: Bearer abc123", "https://example.invalid/v?token=secret"]
        redacted = core.redact_command(command)
        self.assertNotIn("abc123", " ".join(redacted))
        self.assertNotIn("secret", " ".join(redacted))


if __name__ == "__main__":
    unittest.main()
