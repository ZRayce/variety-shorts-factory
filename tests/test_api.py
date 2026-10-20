"""API-level checks for conservative local import and authorization behavior."""

import os
import tempfile
import unittest
import io
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

TEST_DATA = tempfile.TemporaryDirectory(prefix="vsf-api-tests-", dir=Path(__file__).parent)
os.environ.setdefault("VSF_DATA_DIR", TEST_DATA.name)

from app.server import application
from app import main as core


class LocalWorkflowApiTests(unittest.TestCase):
    def setUp(self):
        # API tests use placeholder bytes; real FFprobe behavior is covered by the integration test.
        self.probe_patch = patch.object(core, "probe_media", return_value=None)
        resolver = core.resolve_executable
        self.ffprobe_patch = patch.object(core, "resolve_executable",
            side_effect=lambda configured: None if configured == core.FFPROBE else resolver(configured))
        self.probe_patch.start()
        self.addCleanup(self.probe_patch.stop)
        self.ffprobe_patch.start()
        self.addCleanup(self.ffprobe_patch.stop)

    def call(self, method, path, body=b"", headers=None):
        headers = headers or {}
        parsed = urlsplit(path)
        environ = {"REQUEST_METHOD": method, "PATH_INFO": parsed.path, "QUERY_STRING": parsed.query,
                   "HTTP_HOST": headers.get("Host", "127.0.0.1:8765"), "CONTENT_LENGTH": str(len(body)),
                   "CONTENT_TYPE": headers.get("Content-Type", "application/json"), "wsgi.input": io.BytesIO(body)}
        if "Origin" in headers: environ["HTTP_ORIGIN"] = headers["Origin"]
        if "Range" in headers: environ["HTTP_RANGE"] = headers["Range"]
        result = {}
        chunks = application(environ, lambda status, response_headers: result.update(status=status, headers=response_headers))
        result["body"] = b"".join(chunks)
        result["code"] = int(result["status"].split()[0])
        return result

    def test_local_import_starts_unverified_and_duplicate_is_rejected(self):
        response = self.call("POST", "/api/import?filename=sample.mp4", b"small synthetic test bytes")
        self.assertEqual(response["code"], 200)
        record = json.loads(response["body"])
        self.assertEqual(record["permission_status"], "unverified")
        self.assertEqual(record["status"], "inspection_required")
        persisted = json.loads(self.call("GET", "/api/assets")["body"])
        saved = next(asset for asset in persisted if asset["id"] == record["id"])
        self.assertNotIn("relative_path", saved)
        source = self.call("GET", f"/api/assets/{record['id']}/source")
        self.assertEqual(source["code"], 200)
        self.assertEqual(source["body"], b"small synthetic test bytes")
        ranged = self.call("GET", f"/api/assets/{record['id']}/source", headers={"Range": "bytes=2-5"})
        self.assertEqual(ranged["code"], 206)
        self.assertEqual(ranged["body"], b"all ")
        self.assertIn(("Content-Range", "bytes 2-5/26"), ranged["headers"])
        invalid_range = self.call("GET", f"/api/assets/{record['id']}/source", headers={"Range": "bytes=900-"})
        self.assertEqual(invalid_range["code"], 416)
        duplicate = self.call("POST", "/api/import?filename=copy.mp4", b"small synthetic test bytes")
        self.assertEqual(duplicate["code"], 409)

    def test_upload_requires_a_valid_complete_content_length(self):
        with self.assertRaises(core.APIError) as missing:
            core.import_local_file("missing-length.mp4", io.BytesIO(b"x"), None)
        self.assertEqual(missing.exception.status, 411)
        with self.assertRaises(core.APIError) as incomplete:
            core.import_local_file("short-body.mp4", io.BytesIO(b"x"), "2")
        self.assertEqual(incomplete.exception.status, 400)

    def test_traversal_and_unauthorized_render_are_blocked(self):
        traversal = self.call("POST", "/api/import?filename=..%2Funsafe.mp4", b"x")
        self.assertEqual(traversal["code"], 422)
        imported = json.loads(self.call("POST", "/api/import?filename=unverified.mp4", b"different bytes")["body"])
        render = self.call("POST", f"/api/assets/{imported['id']}/render", json.dumps({"start_seconds": 0, "end_seconds": 4}).encode())
        self.assertEqual(render["code"], 403)

    def test_publishing_is_explicitly_disabled(self):
        response = self.call("GET", "/api/publishing")
        self.assertEqual(response["code"], 200)
        self.assertFalse(json.loads(response["body"])["enabled"])

    def test_youtube_link_intake_records_reference_without_fetching_media(self):
        created = self.call("POST", "/api/youtube-candidates",
                            json.dumps({"url": "https://www.youtube.com/watch?v=AbCdEf12345"}).encode())
        self.assertEqual(created["code"], 201)
        candidate = json.loads(created["body"])
        self.assertEqual(candidate["status"], "media_required")
        self.assertEqual(candidate["url"], "https://www.youtube.com/watch?v=AbCdEf12345")
        listed = self.call("GET", "/api/youtube-candidates")
        self.assertEqual(listed["code"], 200)
        self.assertIn(b"AbCdEf12345", listed["body"])
        invalid = self.call("POST", "/api/youtube-candidates",
                            json.dumps({"url": "https://youtube.com.evil.example/watch?v=AbCdEf12345"}).encode())
        self.assertEqual(invalid["code"], 422)

    def test_health_reports_real_capabilities_without_claiming_ai(self):
        health = json.loads(self.call("GET", "/api/health")["body"])
        self.assertEqual(health["status"], "ok")
        self.assertTrue(health["database_ok"])
        self.assertFalse(health["ai_editing_available"])
        self.assertFalse(health["publishing_enabled"])

    def test_ffmpeg_diagnostics_and_render_test_endpoints_return_structured_results(self):
        with patch.object(core, "diagnose_media_tools", return_value={"ffmpeg": {"status": "Available"},
                                                                         "ffprobe": {"status": "Missing"}}), \
             patch.object(core, "run_ffmpeg_render_test", return_value={"status": "passed", "duration_seconds": 2.0}):
            diagnostics = self.call("POST", "/api/diagnostics")
            render_test = self.call("POST", "/api/diagnostics/render-test")
        self.assertEqual(diagnostics["code"], 200)
        self.assertEqual(json.loads(diagnostics["body"])["ffprobe"]["status"], "Missing")
        self.assertEqual(json.loads(render_test["body"])["status"], "passed")

    def test_project_api_saves_and_reloads_versioned_ordered_timeline(self):
        imported = json.loads(self.call("POST", "/api/import?filename=project-api.mp4", b"project api fixture")['body'])
        creator = json.loads(self.call("POST", "/api/creators", json.dumps({"name": "Project API Creator"}).encode())['body'])
        with core.connect() as db:
            db.execute("UPDATE assets SET duration=20,width=640,height=360,has_audio=1 WHERE id=?", (imported["id"],))
        permission = self.call("POST", f"/api/assets/{imported['id']}/permission", json.dumps({
            "creator_id": creator["id"], "status": "granted", "evidence": "Local project API test permission"}).encode())
        self.assertEqual(permission["code"], 200)
        created = self.call("POST", f"/api/assets/{imported['id']}/projects", json.dumps({
            "title": "Timeline from API", "segments": [{"start_seconds": 2, "end_seconds": 5},
            {"start_seconds": 8, "end_seconds": 11}]}).encode())
        self.assertEqual(created["code"], 201)
        project = json.loads(created["body"])
        self.assertEqual(project["plan"]["schema_version"], 1)
        loaded = self.call("GET", f"/api/projects/{project['id']}")
        self.assertEqual(loaded["code"], 200)
        self.assertEqual([segment["order"] for segment in json.loads(loaded["body"])["plan"]["segments"]], [0, 1])
        versions = self.call("GET", f"/api/projects/{project['id']}/versions")
        self.assertEqual(len(json.loads(versions["body"])), 1)

    def test_candidate_api_reports_filter_failure_as_warning_and_keeps_pending_review(self):
        imported = json.loads(self.call("POST", "/api/import?filename=analysis-api.mp4", b"analysis api fixture")['body'])
        creator = json.loads(self.call("POST", "/api/creators", json.dumps({"name": "Analysis API Creator"}).encode())['body'])
        with core.connect() as db:
            db.execute("UPDATE assets SET duration=20,width=640,height=360,has_audio=0 WHERE id=?", (imported["id"],))
        self.call("POST", f"/api/assets/{imported['id']}/permission", json.dumps({
            "creator_id": creator["id"], "status": "granted", "evidence": "Local candidate analysis test"}).encode())
        with patch.object(core, "analyze_source", return_value={"scene_times": [], "silence_intervals": [],
                                                                  "diagnostics": ["Scene filter unavailable"]}):
            response = self.call("POST", f"/api/assets/{imported['id']}/discover", json.dumps({
                "content_preset": "custom", "minimum_seconds": 4, "maximum_seconds": 8,
                "padding_before": 1, "padding_after": 1}).encode())
        self.assertEqual(response["code"], 201)
        result = json.loads(response["body"])
        self.assertTrue(result["diagnostics"])
        self.assertTrue(all(item["status"] == "pending" for item in result["candidates"]))
        listed = self.call("GET", f"/api/candidates?asset_id={imported['id']}")
        self.assertEqual(listed["code"], 200)
        listed_candidates = json.loads(listed["body"])
        self.assertEqual(len(listed_candidates), len(result["candidates"]))
        self.assertTrue(listed_candidates[0]["score_factors"])

    def test_generated_render_log_is_available_to_the_local_review_ui(self):
        path = core.LOG_ROOT / "api-pilot.log"
        path.write_text("safe local log", encoding="utf-8")
        try:
            response = self.call("GET", f"/api/files/logs/{path.name}")
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(response["code"], 200)
        self.assertEqual(response["body"], b"safe local log")

    def test_web_interface_is_served_and_nonlocal_hosts_are_rejected(self):
        page = self.call("GET", "/")
        self.assertEqual(page["code"], 200)
        self.assertIn(b"ClipForge", page["body"])
        self.assertIn(b"render-profile", page["body"])
        self.assertIn(b"commercial-use", page["body"])
        self.assertIn(b"run-diagnostics", page["body"])
        self.assertIn(b"run-render-test", page["body"])
        blocked = self.call("GET", "/api/health", headers={"Host": "example.org"})
        self.assertEqual(blocked["code"], 403)

    def test_cross_origin_state_change_is_rejected(self):
        blocked = self.call("POST", "/api/creators", json.dumps({"name": "Untrusted"}).encode(),
                            headers={"Origin": "https://example.org"})
        self.assertEqual(blocked["code"], 403)

    def test_concurrent_duplicate_uploads_commit_only_once(self):
        payload = b"same concurrent file contents"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda name: self.call("POST", f"/api/import?filename={name}.mp4", payload),
                                    ["concurrent-a", "concurrent-b"]))
        self.assertEqual(sorted(response["code"] for response in results), [200, 409])

    def test_missing_permission_and_invalid_render_payload_are_rejected(self):
        imported = json.loads(self.call("POST", "/api/import?filename=gate.mp4", b"permission gate bytes")["body"])
        unauthorized = self.call("POST", f"/api/assets/{imported['id']}/render", json.dumps({"start_seconds": 0, "end_seconds": 4}).encode())
        self.assertEqual(unauthorized["code"], 403)
        invalid = self.call("POST", "/api/creators", json.dumps({"name": "A" * 121}).encode())
        self.assertEqual(invalid["code"], 422)

    def test_expired_permission_is_reported_as_unavailable_to_the_editor(self):
        imported = json.loads(self.call("POST", "/api/import?filename=expired.mp4", b"expired permission media")['body'])
        creator = json.loads(self.call("POST", "/api/creators", json.dumps({"name": "Expired Grant Test"}).encode())['body'])
        response = self.call("POST", f"/api/assets/{imported['id']}/permission", json.dumps({
            "creator_id": creator["id"], "status": "granted", "evidence": "Grant has expired",
            "expires_at": "2000-01-01T00:00:00+00:00",
        }).encode())
        self.assertEqual(response["code"], 200)
        assets = json.loads(self.call("GET", "/api/assets")["body"])
        saved = next(asset for asset in assets if asset["id"] == imported["id"])
        self.assertFalse(saved["permission_valid"])


if __name__ == "__main__":
    unittest.main()
