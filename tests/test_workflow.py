"""Workflow regression tests for captions, job idempotency, and state transitions."""

import os
import json
import sqlite3
import tempfile
import threading
import unittest
import subprocess
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

TEST_DATA = tempfile.TemporaryDirectory(prefix="vsf-workflow-tests-", dir=Path(__file__).parent)
os.environ.setdefault("VSF_DATA_DIR", TEST_DATA.name)

from app import main as core


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.creator_id = core.create_creator({"name": "Workflow Test Creator"})["id"]
        cls.asset_id = "workflow-test-asset"
        (core.SOURCE_ROOT / "workflow-test.mp4").write_bytes(b"authorized fixture source")
        with core.connect() as db:
            db.execute("""INSERT OR IGNORE INTO assets(id,filename,relative_path,sha256,size_bytes,duration,width,height,
                         has_audio,creator_id,status,permission_status,permission_evidence,imported_at)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (cls.asset_id, "workflow.mp4", "workflow-test.mp4", "a" * 64, 10, 120, 1920, 1080,
                        1, cls.creator_id, "inspected", "granted", "Written test permission", core.now_iso()))

    def test_srt_accepts_unicode_and_rejects_bad_timing(self):
        valid = "1\n00:00:00,000 --> 00:00:01,200\nMabuhay, 世界!\n"
        self.assertEqual(core.validate_srt(valid, 10), valid.strip())
        with self.assertRaises(ValueError):
            core.validate_srt("1\n00:00:02,000 --> 00:00:01,000\nBackwards", 10)
        with self.assertRaises(ValueError):
            core.validate_srt("1\n00:00:00,000 --> 00:00:11,000\nToo long", 10)

    def test_env_loader_uses_an_allowlist_and_preserves_process_overrides(self):
        config_path = core.DATA_ROOT / "test-settings.env"
        config_path.write_text("VSF_PORT=9999\nPATH=bad\nVSF_FFMPEG=custom-ffmpeg\n", encoding="utf-8")
        try:
            with patch.dict(os.environ, {"VSF_PORT": "8765"}, clear=False):
                os.environ.pop("VSF_FFMPEG", None)
                core.load_project_env(config_path)
                self.assertEqual(os.environ["VSF_PORT"], "8765")
                self.assertEqual(os.environ["VSF_FFMPEG"], "custom-ffmpeg")
                self.assertNotEqual(os.environ.get("PATH"), "bad")
        finally:
            config_path.unlink(missing_ok=True)

    def test_v2_database_migrates_crop_settings_without_losing_jobs(self):
        with tempfile.TemporaryDirectory(prefix="vsf-migration-", dir=Path(__file__).parent) as temporary:
            database_path = Path(temporary) / "legacy.sqlite3"
            db = sqlite3.connect(database_path)
            try:
                db.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
                db.execute("INSERT INTO schema_version(version) VALUES(2)")
                db.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY,status TEXT NOT NULL,captions_srt TEXT NOT NULL DEFAULT '')")
                db.execute("INSERT INTO jobs(id,status,captions_srt) VALUES('legacy-job','review','1\\ncaption')")
                db.commit()
            finally:
                db.close()
            with patch.object(core, "DB_PATH", database_path):
                core.initialize_database()
                with core.connect() as db:
                    job = db.execute("SELECT id,status,captions_srt,crop_x,crop_y FROM jobs WHERE id='legacy-job'").fetchone()
                    version = db.execute("SELECT version FROM schema_version").fetchone()[0]
            self.assertEqual(tuple(job), ("legacy-job", "review", "1\\ncaption", 50, 50))
            self.assertEqual(version, 5)
            self.assertTrue(database_path.with_name("legacy.v2.backup.sqlite3").is_file())

    def test_duplicate_active_render_returns_existing_job(self):
        with core.connect() as db:
            db.execute("DELETE FROM jobs WHERE asset_id=? AND status IN ('queued','running')", (self.asset_id,))
        with patch.object(core.threading.Thread, "start", lambda _self: None):
            first = core.queue_render(self.asset_id, {"start_seconds": 0, "end_seconds": 8})
            second = core.queue_render(self.asset_id, {"start_seconds": 0, "end_seconds": 8})
        self.assertEqual(second["id"], first["id"])
        self.assertTrue(second["already_queued"])

    def test_crop_framing_is_bounded_and_persisted_for_the_render(self):
        with self.assertRaises(core.APIError) as raised:
            core.queue_render(self.asset_id, {"start_seconds": 0, "end_seconds": 8, "crop_x": 101})
        self.assertEqual(raised.exception.status, 422)
        with patch.object(core.threading.Thread, "start", lambda _self: None):
            queued = core.queue_render(self.asset_id, {"start_seconds": 0, "end_seconds": 8,
                                                       "crop_x": 20, "crop_y": 75})
        job = core.get_job(queued["id"])
        self.assertEqual((job["crop_x"], job["crop_y"]), (20, 75))
        with core.connect() as db:
            db.execute("DELETE FROM jobs WHERE id=?", (queued["id"],))

    def test_render_filter_uses_saved_crop_position(self):
        with patch.object(core.threading.Thread, "start", lambda _self: None):
            queued = core.queue_render(self.asset_id, {"start_seconds": 20, "end_seconds": 28,
                                                       "crop_x": 25, "crop_y": 80})
        command_holder = {}

        def fake_ffmpeg(job_id, args, log_path, timeout):
            command_holder["filter"] = args[args.index("-vf") + 1]
            Path(args[-1]).write_bytes(b"verified synthetic output")

        media = {"duration": 8.0, "width": 1080, "height": 1920, "has_audio": True}
        with patch.object(core.shutil, "which", return_value="ffmpeg.exe"), \
             patch.object(core, "run_render_process", side_effect=fake_ffmpeg), \
             patch.object(core, "probe_output", return_value=media):
            core.render_job(queued["id"])
        self.assertIn("(in_w-1080)*0.25", command_holder["filter"])
        self.assertIn("(in_h-1920)*0.8", command_holder["filter"])
        job = core.get_job(queued["id"])
        core.remove_generated_file(core.OUTPUT_ROOT / job["output_path"], "test_output")
        with core.connect() as db:
            db.execute("DELETE FROM jobs WHERE id=?", (queued["id"],))

    def test_review_decision_cannot_be_rewritten(self):
        job_id = "workflow-review-test"
        output = core.OUTPUT_ROOT / "verified.mp4"
        output.write_bytes(b"verified synthetic output")
        with core.connect() as db:
            rights_version = db.execute("SELECT permission_version FROM assets WHERE id=?", (self.asset_id,)).fetchone()[0]
            db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            db.execute("""INSERT INTO jobs(id,asset_id,status,stage,start_seconds,end_seconds,preset,output_path,rights_version,created_at,updated_at)
                         VALUES(?,?,'review','quality_assurance',0,5,'clean_vertical','verified.mp4',?,?,?)""",
                       (job_id, self.asset_id, rights_version, core.now_iso(), core.now_iso()))
        self.assertEqual(core.review_job(job_id, {"decision": "approved"})["status"], "approved")
        with self.assertRaises(core.APIError) as raised:
            core.review_job(job_id, {"decision": "rejected"})
        self.assertEqual(raised.exception.status, 409)
        output.unlink(missing_ok=True)
        with core.connect() as db:
            db.execute("DELETE FROM reviews WHERE job_id=?", (job_id,))
            db.execute("DELETE FROM jobs WHERE id=?", (job_id,))

    def test_retry_requires_safe_failed_job_and_is_bounded(self):
        job_id = "workflow-retry-test"
        with core.connect() as db:
            rights_version = db.execute("SELECT permission_version FROM assets WHERE id=?", (self.asset_id,)).fetchone()[0]
            db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            db.execute("""INSERT INTO jobs(id,asset_id,status,stage,start_seconds,end_seconds,preset,error_category,
                         retry_safe,retry_count,rights_version,created_at,updated_at) VALUES(?,?,'failed','render',0,5,
                         'clean_vertical','missing_dependency',1,0,?,?,?)""",
                       (job_id, self.asset_id, rights_version, core.now_iso(), core.now_iso()))
        with patch.object(core.threading.Thread, "start", lambda _self: None):
            self.assertEqual(core.retry_job(job_id)["status"], "queued")
        with self.assertRaises(core.APIError) as raised:
            core.retry_job(job_id)
        self.assertEqual(raised.exception.status, 409)

    def test_interrupted_jobs_recover_as_retryable_failures(self):
        job_id = "workflow-interrupted-test"
        with core.connect() as db:
            db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            db.execute("""INSERT INTO jobs(id,asset_id,status,stage,start_seconds,end_seconds,preset,created_at,updated_at)
                         VALUES(?,?,'running','render',0,5,'clean_vertical',?,?)""",
                       (job_id, self.asset_id, core.now_iso(), core.now_iso()))
        core.recover_interrupted_jobs()
        with core.connect() as db:
            job = db.execute("SELECT status,error_category,retry_safe FROM jobs WHERE id=?", (job_id,)).fetchone()
            columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
        self.assertEqual((job["status"], job["error_category"], job["retry_safe"]), ("failed", "interrupted", 1))
        self.assertIn("captions_srt", columns)

    def test_queued_jobs_resume_after_restart(self):
        job_id = "workflow-resume-queued-test"
        with core.connect() as db:
            db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            db.execute("""INSERT INTO jobs(id,asset_id,status,stage,start_seconds,end_seconds,preset,created_at,updated_at)
                         VALUES(?,?,'queued','queued',0,5,'clean_vertical',?,?)""",
                       (job_id, self.asset_id, core.now_iso(), core.now_iso()))
        core.recover_interrupted_jobs()
        with core.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()[0], "queued")
        with patch.object(core, "start_render_worker") as start_worker:
            core.resume_queued_jobs()
        start_worker.assert_called_once_with(job_id)
        with core.connect() as db:
            db.execute("DELETE FROM jobs WHERE id=?", (job_id,))

    def test_missing_ffmpeg_becomes_a_persisted_retryable_failure(self):
        with patch.object(core.threading.Thread, "start", lambda _self: None):
            queued = core.queue_render(self.asset_id, {"start_seconds": 10, "end_seconds": 18})
        with patch.object(core.shutil, "which", return_value=None):
            core.render_job(queued["id"])
        job = core.get_job(queued["id"])
        self.assertEqual((job["status"], job["error_category"], job["retry_safe"]),
                         ("failed", "missing_dependency", 1))

    def test_long_form_profile_allows_longer_cuts_and_short_profile_stays_bounded(self):
        with self.assertRaises(core.APIError) as raised:
            core.queue_render(self.asset_id, {"start_seconds": 0, "end_seconds": 61})
        self.assertEqual(raised.exception.status, 422)
        with patch.object(core.threading.Thread, "start", lambda _self: None):
            queued = core.queue_render(self.asset_id, {"start_seconds": 0, "end_seconds": 90,
                                                       "profile": "long_horizontal"})
        self.assertEqual(core.get_job(queued["id"])["profile"], "long_horizontal")
        with core.connect() as db:
            db.execute("DELETE FROM jobs WHERE id=?", (queued["id"],))

    def test_queued_job_can_be_cancelled_without_starting_a_worker(self):
        with patch.object(core.threading.Thread, "start", lambda _self: None):
            queued = core.queue_render(self.asset_id, {"start_seconds": 0, "end_seconds": 8})
        self.assertEqual(core.cancel_job(queued["id"])["status"], "cancelled")
        self.assertEqual(core.get_job(queued["id"])["status"], "cancelled")
        with self.assertRaises(core.APIError) as raised:
            core.cancel_job(queued["id"])
        self.assertEqual(raised.exception.status, 409)
        with core.connect() as db:
            db.execute("DELETE FROM jobs WHERE id=?", (queued["id"],))

    def test_running_ffmpeg_process_is_terminated_on_cancel_request(self):
        core.record_permission(self.asset_id, {"creator_id": self.creator_id, "status": "granted",
                                               "evidence": "Test fixture permission restored"})
        with patch.object(core.threading.Thread, "start", lambda _self: None):
            queued = core.queue_render(self.asset_id, {"start_seconds": 0, "end_seconds": 8})
        with core.connect() as db:
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (queued["id"],))
        started = threading.Event()
        terminated = threading.Event()

        class FakeProcess:
            def __init__(self):
                self.returncode = None

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                started.set()
                if not terminated.wait(timeout):
                    raise subprocess.TimeoutExpired("ffmpeg", timeout)
                return self.returncode

            def terminate(self):
                self.returncode = -15
                terminated.set()

            def kill(self):
                self.terminate()

        process = FakeProcess()
        log_path = core.LOG_ROOT / f"{queued['id']}.log"
        worker = threading.Thread(target=core.run_render_process,
                                  args=(queued["id"], ["ffmpeg"], log_path, 5))
        with patch.object(core.subprocess, "Popen", return_value=process):
            worker.start()
            self.assertTrue(started.wait(2))
            self.assertEqual(core.cancel_job(queued["id"])["status"], "cancelling")
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertTrue(terminated.is_set())
        core.finish_cancelled_render(queued["id"], "Test cancellation completed.")
        self.assertEqual(core.get_job(queued["id"])["status"], "cancelled")
        log_path.unlink(missing_ok=True)
        with core.connect() as db:
            db.execute("DELETE FROM jobs WHERE id=?", (queued["id"],))

    def test_commercial_render_requires_a_separate_current_grant(self):
        with self.assertRaises(core.APIError) as raised:
            core.queue_render(self.asset_id, {"start_seconds": 0, "end_seconds": 8, "commercial_use": True})
        self.assertEqual(raised.exception.status, 403)
        core.record_permission(self.asset_id, {"creator_id": self.creator_id, "status": "granted",
                                               "evidence": "Editing permission scope",
                                               "commercial_status": "granted",
                                               "commercial_evidence": "Commercial license scope"})
        with patch.object(core.threading.Thread, "start", lambda _self: None):
            queued = core.queue_render(self.asset_id, {"start_seconds": 0, "end_seconds": 8, "commercial_use": True})
        self.assertTrue(core.get_job(queued["id"])["commercial_use"])
        with core.connect() as db:
            db.execute("DELETE FROM jobs WHERE id=?", (queued["id"],))

    def test_youtube_link_is_saved_as_media_required_candidate_only(self):
        candidate = core.add_youtube_candidate({"url": "https://youtu.be/AbCdEf12345"})
        self.assertEqual(candidate["video_id"], "AbCdEf12345")
        self.assertEqual(candidate["status"], "media_required")
        self.assertNotIn("local_media_path", candidate)
        self.assertEqual(core.list_youtube_candidates()[0]["id"], candidate["id"])

    def test_revoked_rights_block_approval_and_permission_updates_are_versioned(self):
        output = core.OUTPUT_ROOT / "rights-revocation-test.mp4"
        output.write_bytes(b"verified output")
        job_id = "rights-revocation-test"
        with core.connect() as db:
            rights_version = db.execute("SELECT permission_version FROM assets WHERE id=?", (self.asset_id,)).fetchone()[0]
            db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            db.execute("""INSERT INTO jobs(id,asset_id,status,stage,start_seconds,end_seconds,preset,output_path,rights_version,created_at,updated_at)
                         VALUES(?,?,'review','quality_assurance',0,5,'clean_vertical',?,?,?,?)""",
                       (job_id, self.asset_id, output.name, rights_version, core.now_iso(), core.now_iso()))
        revised = core.record_permission(self.asset_id, {
            "creator_id": self.creator_id, "status": "revoked", "evidence": "Creator revoked permission"
        })
        self.assertGreaterEqual(revised["permission_version"], 1)
        with self.assertRaises(core.APIError) as raised:
            core.review_job(job_id, {"decision": "approved"})
        self.assertEqual(raised.exception.status, 403)
        with core.connect() as db:
            row = db.execute("SELECT permission_status,version FROM permission_history WHERE asset_id=? ORDER BY version DESC LIMIT 1",
                             (self.asset_id,)).fetchone()
        self.assertEqual(tuple(row), ("revoked", revised["permission_version"]))
        with core.connect() as db:
            db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        output.unlink(missing_ok=True)

    def test_mocked_render_verifies_output_and_burns_unicode_srt(self):
        captions = "1\n00:00:00,000 --> 00:00:01,200\nMabuhay, 世界!"
        with patch.object(core.threading.Thread, "start", lambda _self: None):
            queued = core.queue_render(self.asset_id, {"start_seconds": 20, "end_seconds": 28, "captions_srt": captions})
        command_holder = {}

        def fake_ffmpeg(job_id, args, log_path, timeout):
            command_holder["command"] = args
            srt_path = core.OUTPUT_ROOT / f"{queued['id']}.srt"
            self.assertTrue(srt_path.is_file())
            self.assertIn("subtitles=", args[args.index("-vf") + 1])
            Path(args[-1]).write_bytes(b"verified synthetic output")

        media = {"duration": 8.0, "width": 1080, "height": 1920, "has_audio": True}
        with patch.object(core.shutil, "which", return_value="ffmpeg.exe"), \
             patch.object(core, "run_render_process", side_effect=fake_ffmpeg), \
             patch.object(core, "probe_output", return_value=media):
            core.render_job(queued["id"])
        job = core.get_job(queued["id"])
        self.assertEqual(job["status"], "review")
        self.assertEqual(job["captions_srt"], captions)
        self.assertEqual(job["retry_safe"], 0)
        report = json.loads(job["quality_report"])
        self.assertEqual(report["quality_status"], "PASS")
        self.assertTrue(all(check["status"] in {"PASS", "NOT_APPLICABLE", "WARN"} for check in report["checks"]))
        self.assertFalse((core.OUTPUT_ROOT / f"{queued['id']}.srt").exists())
        self.assertTrue((core.OUTPUT_ROOT / job["output_path"]).exists())
        core.remove_generated_file(core.OUTPUT_ROOT / job["output_path"], "test_output")

    def test_long_form_render_uses_widescreen_canvas_and_quality_gate(self):
        with patch.object(core.threading.Thread, "start", lambda _self: None):
            queued = core.queue_render(self.asset_id, {"start_seconds": 0, "end_seconds": 90,
                                                       "profile": "long_horizontal"})
        captured = {}

        def fake_process(job_id, command, log_path, timeout):
            captured["filter"] = command[command.index("-vf") + 1]
            Path(command[-1]).write_bytes(b"verified synthetic widescreen output")

        media = {"duration": 90.0, "width": 1920, "height": 1080, "has_audio": True}
        with patch.object(core.shutil, "which", return_value="ffmpeg.exe"), \
             patch.object(core, "run_render_process", side_effect=fake_process), \
             patch.object(core, "probe_output", return_value=media):
            core.render_job(queued["id"])
        job = core.get_job(queued["id"])
        self.assertEqual(job["status"], "review")
        self.assertIn("scale=1920:1080", captured["filter"])
        self.assertEqual(json.loads(job["quality_report"])["width"], 1920)
        core.remove_generated_file(core.OUTPUT_ROOT / job["output_path"], "test_output")
        with core.connect() as db:
            db.execute("DELETE FROM jobs WHERE id=?", (queued["id"],))

    def test_quality_failure_discards_partial_output_and_never_marks_review(self):
        with patch.object(core.threading.Thread, "start", lambda _self: None):
            queued = core.queue_render(self.asset_id, {"start_seconds": 0, "end_seconds": 8})

        def fake_process(job_id, command, log_path, timeout):
            Path(command[-1]).write_bytes(b"video missing source audio")

        invalid = {"duration": 8.0, "width": 1080, "height": 1920, "has_audio": False}
        with patch.object(core.shutil, "which", return_value="ffmpeg.exe"), \
             patch.object(core, "run_render_process", side_effect=fake_process), \
             patch.object(core, "probe_output", return_value=invalid):
            core.render_job(queued["id"])
        job = core.get_job(queued["id"])
        self.assertEqual(job["status"], "failed")
        self.assertIsNone(job["output_path"])
        self.assertEqual(list(core.OUTPUT_ROOT.glob(".*.partial.mp4")), [])
        with core.connect() as db:
            db.execute("DELETE FROM jobs WHERE id=?", (queued["id"],))

    def test_existing_output_collision_is_preserved(self):
        with patch.object(core.threading.Thread, "start", lambda _self: None):
            queued = core.queue_render(self.asset_id, {"start_seconds": 0, "end_seconds": 8})
        output = core.OUTPUT_ROOT / f"{queued['id']}_short_vertical.mp4"
        output.write_bytes(b"user-owned existing output")
        with patch.object(core.shutil, "which", return_value="ffmpeg.exe"):
            core.render_job(queued["id"])
        self.assertEqual(output.read_bytes(), b"user-owned existing output")
        self.assertEqual(core.get_job(queued["id"])["status"], "failed")
        output.unlink(missing_ok=True)
        with core.connect() as db:
            db.execute("DELETE FROM jobs WHERE id=?", (queued["id"],))

    def test_low_disk_space_blocks_render_before_ffmpeg_starts(self):
        with patch.object(core.threading.Thread, "start", lambda _self: None):
            queued = core.queue_render(self.asset_id, {"start_seconds": 0, "end_seconds": 8})
        with patch.object(core.shutil, "which", return_value="ffmpeg.exe"), \
             patch.object(core.shutil, "disk_usage", return_value=SimpleNamespace(free=1)), \
             patch.object(core, "run_render_process") as render_process:
            core.render_job(queued["id"])
        self.assertFalse(render_process.called)
        job = core.get_job(queued["id"])
        self.assertEqual(job["status"], "failed")
        self.assertIn("Not enough free disk space", job["diagnostic"])
        with core.connect() as db:
            db.execute("DELETE FROM jobs WHERE id=?", (queued["id"],))

    def test_render_log_tail_is_bounded(self):
        with tempfile.TemporaryDirectory(prefix="vsf-log-test-", dir=Path(__file__).parent) as temporary:
            path = Path(temporary) / "render.log"
            path.write_text("a" * 5000 + "END", encoding="utf-8")
            tail = core.read_log_tail(path, 64)
        self.assertEqual(len(tail), 64)
        self.assertTrue(tail.endswith("END"))


if __name__ == "__main__":
    unittest.main()
